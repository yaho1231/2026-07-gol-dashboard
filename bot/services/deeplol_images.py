"""디스코드 임베드용 DeepLOL 이미지 URL 헬퍼.

대시보드(image_assets.py)는 이미지를 로컬에 캐시해 data URI로 임베드하지만,
디스코드 임베드는 **공개 URL만** 받는다(디스코드 서버가 직접 이미지를 가져감).
따라서 여기서는 DeepLOL CDN 공개 URL 문자열을 그대로 만들어 돌려준다.

지원 포맷: PNG · JPG · GIF · WebP (디스코드는 SVG 임베드를 렌더하지 못하므로
라인 SVG 아이콘은 URL로 넣지 않는다 — 텍스트/이모지로 대체).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import ssl
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_DATA_DIR = os.path.join(_REPO_ROOT, "backend", "app", "data")

DEEPLOL_STATIC_BASE = "https://www.deeplol.gg/images"
DEEPLOL_CDN_BASE = "https://ak-deeplol-ddragon-cdn.deeplol.gg/cdn"
# 챔피언 이미지 버전은 DDragon versions.json에서 최신([0])을 실시간 조회한다.
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
FALLBACK_CHAMPION_CDN_VERSION = "16.13.1"
_VERSION_TTL_SECONDS = 6 * 3600
_SSL_CONTEXT = ssl._create_unverified_context()
_VERSION_CACHE: dict[str, object] = {"version": None, "fetched_at": 0.0}


def _fetch_latest_version_blocking() -> str | None:
    """DDragon versions.json에서 최신 버전을 '동기'로 조회한다.

    urlopen 은 최대 10초까지 블로킹되므로, 이벤트 루프가 도는 봇 프로세스에서는
    반드시 스레드(refresh_champion_version → asyncio.to_thread)에서만 호출해야 한다.
    """
    try:
        request = urllib.request.Request(
            DDRAGON_VERSIONS_URL, headers={"User-Agent": "deeplolDiscord/1.0"}
        )
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as response:
            versions = json.loads(response.read().decode("utf-8"))
        if versions:
            return str(versions[0])
    except Exception:
        return None
    return None


def latest_champion_version() -> str:
    """캐시된 최신 게임 버전(예: '16.13.1')을 '즉시(논블로킹)' 반환한다.

    ⚠️ 이벤트 루프 블로킹 방지: 이 함수는 절대 네트워크를 호출하지 않는다.
    async 명령 핸들러(임베드 썸네일 생성 등)에서 호출되므로, 여기서 동기 urlopen 을
    돌리면 캐시 미스/TTL 만료 순간 이벤트 루프가 최대 10초 얼어붙어, 그 사이 도착한
    다른 인터랙션이 3초 내 defer 하지 못하고 'Unknown interaction'(10062)으로 터진다.
    실제 버전 갱신은 refresh_champion_version()(백그라운드 tasks 루프)이 담당한다.
    캐시가 비어 있으면 마지막 알려진 값(없으면 폴백 상수)을 그대로 돌려준다.
    """
    return str(_VERSION_CACHE.get("version") or FALLBACK_CHAMPION_CDN_VERSION)


async def refresh_champion_version() -> str:
    """DDragon 최신 버전을 백그라운드 스레드에서 조회해 캐시를 갱신한다(논블로킹).

    동기 urlopen 을 asyncio.to_thread 로 넘겨 이벤트 루프를 막지 않는다.
    봇 기동 시 1회 + 주기적(tasks 루프)으로 호출해 캐시를 항상 채워둔다.
    """
    version = await asyncio.to_thread(_fetch_latest_version_blocking)
    if version:
        _VERSION_CACHE["version"] = version
        _VERSION_CACHE["fetched_at"] = time.time()
    return latest_champion_version()

_TIER_EMBLEMS = {
    "UNRANKED", "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
}

_CHAMPION_KEY_BY_NAME: dict[str, str] | None = None


def _version_sort_key(path: str) -> tuple[int, ...]:
    version = os.path.basename(path).replace("champion_ko_KR_", "").replace(".json", "")
    parts: list[int] = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _load_champion_key_map() -> dict[str, str]:
    """DDragon 챔피언 JSON에서 한글 이름 → 이미지 키(예: 'LeeSin') 맵을 만든다."""
    global _CHAMPION_KEY_BY_NAME
    if _CHAMPION_KEY_BY_NAME is not None:
        return _CHAMPION_KEY_BY_NAME

    mapping: dict[str, str] = {}
    # 버전 숫자 기준 정렬 → 마지막이 최신(16.9.1 < 16.13.1을 올바르게 판정).
    files = sorted(glob.glob(os.path.join(_DATA_DIR, "champion_ko_KR_*.json")), key=_version_sort_key)
    if files:
        try:
            with open(files[-1], encoding="utf-8") as fp:
                payload = json.load(fp)
            for champ in payload.get("data", {}).values():
                name = champ.get("name")
                image_key = champ.get("id")
                if name and image_key:
                    mapping[str(name)] = str(image_key)
        except Exception:
            mapping = {}
    _CHAMPION_KEY_BY_NAME = mapping
    return mapping


def champion_image_url(champion_name: str | None) -> str | None:
    """챔피언 한글명(예: '리 신')으로 64px 챔피언 아이콘 CDN URL을 반환. 실패 시 None."""
    if not champion_name:
        return None
    key = _load_champion_key_map().get(str(champion_name).strip())
    if not key:
        return None
    return f"{DEEPLOL_CDN_BASE}/{latest_champion_version()}/img/champion/{key}__64.webp"


def tier_emblem_url(tier: str | None) -> str | None:
    """솔로랭크 티어 문자열('EMERALD', 'EMERALD IV' 등)로 엠블럼 PNG URL을 반환."""
    if not tier:
        return None
    tier_key = str(tier).strip().upper().split()[0]
    if tier_key not in _TIER_EMBLEMS:
        return None
    return f"{DEEPLOL_STATIC_BASE}/Emblem_{tier_key}.png"


def profile_icon_url(icon_id: int | None) -> str | None:
    """소환사 프로필 아이콘 ID로 100px 프로필 아이콘 CDN URL을 반환."""
    if not icon_id:
        return None
    try:
        return f"{DEEPLOL_CDN_BASE}/img/profileicon/{int(icon_id)}__100.webp"
    except (TypeError, ValueError):
        return None
