"""DeepLOL CDN 이미지(티어 엠블럼·포지션 아이콘·챔피언·프로필)를 로컬에 캐시하고 data URI로 반환.

- 최초 1회만 다운로드해 dashboard/assets/img_cache/ 에 저장한다.
- HTML/표 임베드용 base64 data URI를 돌려주므로 공개 대시보드가 외부 CDN에 의존하지 않는다.
- 티어 엠블럼 PNG는 원본이 100~230KB라 저장 시 64px로 축소한다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import ssl
import time
import urllib.request
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent / "assets" / "img_cache"

DEEPLOL_STATIC_BASE = "https://www.deeplol.gg/images"
DEEPLOL_CDN_BASE = "https://ak-deeplol-ddragon-cdn.deeplol.gg/cdn"
# 챔피언 이미지 버전은 DDragon versions.json에서 최신([0])을 실시간 조회한다.
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
FALLBACK_CHAMPION_CDN_VERSION = "16.13.1"
_VERSION_TTL_SECONDS = 6 * 3600
_VERSION_CACHE: dict[str, object] = {"version": None, "fetched_at": 0.0}

# 유효한 티어 엠블럼 파일명 (Emblem_{TIER}.png)
TIER_EMBLEMS = {
    "UNRANKED", "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
}

# 표준 포지션 → DeepLOL 아이콘 파일명 (mid/adc는 존재하지 않음 — middle/bot 사용)
POSITION_ICON_NAMES = {
    "TOP": "top",
    "JUNGLE": "jungle",
    "MIDDLE": "middle",
    "BOTTOM": "bot",
    "UTILITY": "supporter",
}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_SSL_CONTEXT = ssl._create_unverified_context()
_URI_CACHE: dict[tuple[str, int | None], str | None] = {}


def latest_champion_version() -> str:
    """DDragon에서 최신 게임 버전(예: '16.13.1')을 조회. 6시간 캐시, 실패 시 폴백."""
    now = time.time()
    cached = _VERSION_CACHE.get("version")
    if cached and now - float(_VERSION_CACHE.get("fetched_at", 0.0)) < _VERSION_TTL_SECONDS:
        return str(cached)
    version: str | None = None
    try:
        request = urllib.request.Request(
            DDRAGON_VERSIONS_URL, headers={"User-Agent": "deeplolDiscord/1.0"}
        )
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as response:
            versions = json.loads(response.read().decode("utf-8"))
        if versions:
            version = str(versions[0])
    except Exception:
        version = None
    version = version or str(cached or FALLBACK_CHAMPION_CDN_VERSION)
    _VERSION_CACHE["version"] = version
    _VERSION_CACHE["fetched_at"] = now
    return version


def _cache_path(url: str) -> Path:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    digest = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in (stem or name))
    return CACHE_DIR / f"{safe_stem}_{digest}.{ext if dot else 'bin'}"


def _download(url: str) -> bytes | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "deeplolDiscord/1.0"})
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
        # deeplol.gg는 없는 파일도 SPA HTML(200)로 응답하므로 콘텐츠 타입으로 걸러낸다.
        if "text/html" in content_type:
            return None
        return data
    except Exception:
        return None


def _shrink_png(data: bytes, max_px: int) -> bytes:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if max(img.size) <= max_px:
            return data
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return data


def image_data_uri(url: str, max_px: int | None = None) -> str | None:
    """URL 이미지를 로컬 캐시 후 data URI로 반환. 실패 시 None."""
    cache_key = (url, max_px)
    if cache_key in _URI_CACHE:
        return _URI_CACHE[cache_key]

    path = _cache_path(url)
    ext = path.suffix.lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")

    data: bytes | None = None
    if path.is_file():
        data = path.read_bytes()
    else:
        data = _download(url)
        if data is not None:
            if max_px and ext == ".png":
                data = _shrink_png(data, max_px)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    uri = f"data:{mime};base64,{base64.b64encode(data).decode()}" if data else None
    _URI_CACHE[cache_key] = uri
    return uri


def tier_emblem_uri(tier: str | None) -> str | None:
    """솔로랭크 티어 문자열('EMERALD', 'EMERALD IV' 등)로 엠블럼 data URI 반환."""
    if not tier:
        return None
    tier_key = str(tier).strip().upper().split()[0]
    if tier_key not in TIER_EMBLEMS:
        return None
    return image_data_uri(f"{DEEPLOL_STATIC_BASE}/Emblem_{tier_key}.png", max_px=64)


def position_icon_uri(position: str | None) -> str | None:
    """표준 포지션(TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY)의 라인 아이콘 data URI 반환."""
    icon_name = POSITION_ICON_NAMES.get(str(position or "").strip().upper())
    if not icon_name:
        return None
    return image_data_uri(f"{DEEPLOL_STATIC_BASE}/icon_s_position_{icon_name}.svg")


def champion_icon_uri(image_key: str | None) -> str | None:
    """챔피언 이미지 키(예: 'LeeSin')로 64px 챔피언 아이콘 data URI 반환."""
    if not image_key:
        return None
    return image_data_uri(
        f"{DEEPLOL_CDN_BASE}/{latest_champion_version()}/img/champion/{image_key}__64.webp"
    )


def profile_icon_uri(icon_id: int | None) -> str | None:
    """소환사 프로필 아이콘 ID로 100px 프로필 아이콘 data URI 반환."""
    if not icon_id:
        return None
    return image_data_uri(f"{DEEPLOL_CDN_BASE}/img/profileicon/{int(icon_id)}__100.webp")
