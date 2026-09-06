"""Riot Data Dragon champion ID to name resolver."""

from __future__ import annotations

import json
import ssl
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import pandas as pd

# 최신 게임 버전은 DDragon versions.json(내림차순, [0]이 최신)에서 실시간으로 조회한다.
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_JSON_URL_TEMPLATE = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/ko_KR/champion.json"
)
# 네트워크 조회가 모두 실패했을 때만 쓰는 최후 폴백 버전.
FALLBACK_DDRAGON_VERSION = "16.13.1"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_VERSION_TTL_SECONDS = 6 * 3600  # 최신 버전 조회 결과를 프로세스 내 6시간 캐시.

_SSL_CONTEXT = ssl._create_unverified_context()
_CHAMPION_BY_ID: dict[int, str] | None = None
_CHAMPION_PAYLOAD: dict | None = None
_IMAGE_KEY_BY_ID: dict[int, str] | None = None
_IMAGE_KEY_BY_NAME: dict[str, str] | None = None
_VERSION_CACHE: dict[str, object] = {"version": None, "fetched_at": 0.0}


def _version_sort_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _local_champion_files() -> list[Path]:
    """data/champion_ko_KR_*.json 파일을 버전 오름차순으로 정렬해 반환(마지막이 최신)."""
    files = list(DATA_DIR.glob("champion_ko_KR_*.json"))
    return sorted(
        files,
        key=lambda p: _version_sort_key(p.stem.replace("champion_ko_KR_", "")),
    )


def _newest_local_version() -> str | None:
    files = _local_champion_files()
    if not files:
        return None
    return files[-1].stem.replace("champion_ko_KR_", "") or None


def get_latest_version() -> str:
    """DDragon에서 최신 게임 버전(예: '16.13.1')을 조회한다.

    프로세스 내에서 6시간 캐시하며, 네트워크 실패 시 로컬 JSON에서 추출한
    최신 버전 → 폴백 상수 순으로 사용한다.
    """
    now = time.time()
    cached = _VERSION_CACHE.get("version")
    if cached and now - float(_VERSION_CACHE.get("fetched_at", 0.0)) < _VERSION_TTL_SECONDS:
        return str(cached)

    version: str | None = None
    try:
        request = urllib.request.Request(
            DDRAGON_VERSIONS_URL,
            headers={"User-Agent": "deeplolDiscord/1.0"},
        )
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as response:
            versions = json.loads(response.read().decode("utf-8"))
        if versions:
            version = str(versions[0])
    except Exception:
        version = None

    if not version:
        version = _newest_local_version() or FALLBACK_DDRAGON_VERSION

    _VERSION_CACHE["version"] = version
    _VERSION_CACHE["fetched_at"] = now
    return version


def _extract_champion_map(payload: dict) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for champ in payload.get("data", {}).values():
        key = champ.get("key")
        name = champ.get("name")
        if key is not None and name:
            mapping[int(key)] = str(name)
    return mapping


def _load_local_payload() -> dict:
    """가장 최신 버전의 로컬 champion_ko_KR_*.json을 읽는다(오프라인 폴백)."""
    for path in reversed(_local_champion_files()):
        try:
            with path.open(encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            continue
    return {}


def _save_payload_to_disk(version: str, payload: dict) -> None:
    """조회한 챔피언 JSON을 로컬에 캐시해 오프라인 폴백과 봇의 이름 맵을 최신화한다."""
    if not payload.get("data"):
        return
    target = DATA_DIR / f"champion_ko_KR_{version}.json"
    if target.exists():
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
    except Exception:
        pass


def _get_payload() -> dict:
    global _CHAMPION_PAYLOAD
    if _CHAMPION_PAYLOAD is not None:
        return _CHAMPION_PAYLOAD
    version = get_latest_version()
    try:
        request = urllib.request.Request(
            CHAMPION_JSON_URL_TEMPLATE.format(version=version),
            headers={"User-Agent": "deeplolDiscord/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20, context=_SSL_CONTEXT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("data"):
            _save_payload_to_disk(version, payload)
            _CHAMPION_PAYLOAD = payload
        else:
            _CHAMPION_PAYLOAD = _load_local_payload()
    except Exception:
        _CHAMPION_PAYLOAD = _load_local_payload()
    return _CHAMPION_PAYLOAD


def _load_local_champion_map() -> dict[int, str]:
    return _extract_champion_map(_load_local_payload())


def _fetch_champion_map() -> dict[int, str]:
    return _extract_champion_map(_get_payload())


def _build_image_key_maps() -> None:
    # DDragon 챔피언 항목의 "id"(예: LeeSin)가 CDN 이미지 파일명 키다.
    global _IMAGE_KEY_BY_ID, _IMAGE_KEY_BY_NAME
    by_id: dict[int, str] = {}
    by_name: dict[str, str] = {}
    for champ in _get_payload().get("data", {}).values():
        key = champ.get("key")
        image_key = champ.get("id")
        name = champ.get("name")
        if not image_key:
            continue
        if key is not None:
            by_id[int(key)] = str(image_key)
        if name:
            by_name[str(name)] = str(image_key)
    _IMAGE_KEY_BY_ID = by_id
    _IMAGE_KEY_BY_NAME = by_name


def get_champion_image_key(champion_id: int | None = None, champion_name: str | None = None) -> str | None:
    """챔피언 ID 또는 한글 이름으로 CDN 이미지 키(예: 'LeeSin')를 찾는다."""
    if _IMAGE_KEY_BY_ID is None or _IMAGE_KEY_BY_NAME is None:
        _build_image_key_maps()
    if champion_id:
        found = _IMAGE_KEY_BY_ID.get(int(champion_id))
        if found:
            return found
    if champion_name:
        return _IMAGE_KEY_BY_NAME.get(str(champion_name).strip())
    return None


def get_champion_map() -> dict[int, str]:
    global _CHAMPION_BY_ID
    if _CHAMPION_BY_ID is None:
        _CHAMPION_BY_ID = _fetch_champion_map()
    return _CHAMPION_BY_ID


def get_champion_name(champion_id: int) -> str:
    if not champion_id:
        return "알 수 없음"
    name = get_champion_map().get(int(champion_id))
    return name or f"챔피언 #{int(champion_id)}"


def resolve_champion_name(champion_id: int, champion_name: Optional[str] = None) -> str:
    if champion_id:
        name = get_champion_map().get(int(champion_id))
        if name:
            return name

    name = (champion_name or "").strip()
    if name and not name.isdigit():
        return name
    return get_champion_name(champion_id)


def enrich_champion_names(df: "pd.DataFrame") -> "pd.DataFrame":
    import pandas as pd

    if df.empty or "champion_id" not in df.columns:
        return df

    enriched = df.copy()
    enriched["champion_name"] = enriched.apply(
        lambda row: resolve_champion_name(
            int(row.get("champion_id") or 0),
            row.get("champion_name"),
        ),
        axis=1,
    )
    return enriched
