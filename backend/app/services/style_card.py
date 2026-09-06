"""플레이스타일 카드 — 공유용 PNG 이미지 생성.

/스타일카드 명령이 사용한다. analyze_player 리포트에서 스타일·지표를 뽑아
시그니처 챔피언 스플래시 아트 배경 위에 트레이딩 카드 형태로 렌더링한다.

- 폰트: macOS 내장 AppleSDGothicNeo(한글). 이모지는 Apple Color Emoji 비트맵을
  래스터라이즈하며, 실패하면 자연스럽게 생략된다.
- 스플래시/아이콘은 DDragon CDN에서 받아 backend/app/data/img_cache/ 에 캐시한다.
"""

from __future__ import annotations

import glob
import io
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config import public_host

KST = timezone(timedelta(hours=9))

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_HERE), "data")
_CACHE_DIR = os.path.join(_DATA_DIR, "img_cache")

W, H = 1200, 675
MARGIN = 64
PANEL_WIDTH = 620  # 왼쪽 텍스트 패널 폭

GOTHIC_TTC = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
GOTHIC_FALLBACK = "/System/Library/Fonts/Supplemental/AppleGothic.ttf"
EMOJI_TTC = "/System/Library/Fonts/Apple Color Emoji.ttc"

# ── 스타일 메타 (이모지 · 강조색 · 태그라인) ────────────────────────────
STYLE_META: dict[str, dict[str, Any]] = {
    "하드캐리형": {"emoji": "👑", "accent": (245, 197, 66),
                "tagline": "팀을 등에 업고 게임을 끝내는 타입"},
    "폭딜누커형": {"emoji": "💥", "accent": (255, 90, 90),
                "tagline": "한 번의 교전으로 게임을 바꾸는 타입"},
    "운영파밍형": {"emoji": "🌾", "accent": (110, 231, 160),
                "tagline": "조용히 성장해 어느새 괴물이 되는 타입"},
    "한타지향형": {"emoji": "⚔️", "accent": (90, 167, 255),
                "tagline": "5대5 한타에서 존재감이 폭발하는 타입"},
    "이니시에이터형": {"emoji": "🚀", "accent": (183, 140, 255),
                  "tagline": "몸부터 던져 싸움의 판을 여는 타입"},
    "시야장악형": {"emoji": "👁️", "accent": (79, 216, 210),
                "tagline": "맵을 밝혀 팀의 눈이 되어주는 타입"},
    "철벽안정형": {"emoji": "🛡️", "accent": (174, 185, 207),
                "tagline": "쉽게 죽지 않는 단단한 플레이가 무기인 타입"},
    "멀티킬하이라이트형": {"emoji": "🔥", "accent": (255, 122, 200),
                    "tagline": "하이라이트 클립을 찍어내는 타입"},
}
DEFAULT_ACCENT = (102, 252, 241)


# ── 폰트 ────────────────────────────────────────────────────────────────
_FONT_INDEX_CACHE: dict[str, int] | None = None
_WEIGHT_FALLBACKS = {
    "Heavy": ["Heavy", "ExtraBold", "Bold", "Regular"],
    "ExtraBold": ["ExtraBold", "Bold", "Heavy", "Regular"],
    "Bold": ["Bold", "SemiBold", "Medium", "Regular"],
    "SemiBold": ["SemiBold", "Bold", "Medium", "Regular"],
    "Medium": ["Medium", "Regular"],
    "Regular": ["Regular", "Medium"],
}


def _font_index_map() -> dict[str, int]:
    """AppleSDGothicNeo.ttc 안의 웨이트별 폰트 인덱스를 탐색해 캐시한다."""
    global _FONT_INDEX_CACHE
    if _FONT_INDEX_CACHE is not None:
        return _FONT_INDEX_CACHE
    mapping: dict[str, int] = {}
    for i in range(16):
        try:
            f = ImageFont.truetype(GOTHIC_TTC, 12, index=i)
            mapping[f.getname()[1]] = i
        except (OSError, IOError):
            break
    _FONT_INDEX_CACHE = mapping
    return mapping


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    indexes = _font_index_map()
    for candidate in _WEIGHT_FALLBACKS.get(weight, ["Regular"]):
        if candidate in indexes:
            return ImageFont.truetype(GOTHIC_TTC, size, index=indexes[candidate])
    try:
        return ImageFont.truetype(GOTHIC_FALLBACK, size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _emoji_image(char: str, size: int) -> Image.Image | None:
    """Apple Color Emoji 를 RGBA 이미지로 래스터라이즈한다. 실패 시 None."""
    for bitmap_size in (160, 137, 96, 64):
        try:
            font = ImageFont.truetype(EMOJI_TTC, bitmap_size)
            canvas = Image.new("RGBA", (bitmap_size * 2, bitmap_size * 2), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            draw.text((bitmap_size // 2, bitmap_size // 2), char, font=font, embedded_color=True)
            bbox = canvas.getbbox()
            if not bbox:
                continue
            cropped = canvas.crop(bbox)
            ratio = size / max(cropped.size)
            return cropped.resize(
                (max(1, int(cropped.width * ratio)), max(1, int(cropped.height * ratio))),
                Image.LANCZOS,
            )
        except (OSError, IOError, ValueError):
            continue
    return None


# ── 챔피언 이미지 (DDragon) ──────────────────────────────────────────────
_CHAMPION_KEY_BY_NAME: dict[str, str] | None = None


def _champion_key_map() -> dict[str, str]:
    """한글 챔피언명 → DDragon 이미지 키('아리' → 'Ahri')."""
    global _CHAMPION_KEY_BY_NAME
    if _CHAMPION_KEY_BY_NAME is not None:
        return _CHAMPION_KEY_BY_NAME
    mapping: dict[str, str] = {}
    files = sorted(glob.glob(os.path.join(_DATA_DIR, "champion_ko_KR_*.json")))
    if files:
        try:
            with open(files[-1], encoding="utf-8") as fp:
                payload = json.load(fp)
            for champ in payload.get("data", {}).values():
                if champ.get("name") and champ.get("id"):
                    mapping[str(champ["name"])] = str(champ["id"])
        except (OSError, json.JSONDecodeError):
            mapping = {}
    _CHAMPION_KEY_BY_NAME = mapping
    return mapping


def _fetch_cached(url: str, filename: str) -> Image.Image | None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, filename)
    if not os.path.exists(path):
        try:
            # macOS python 빌드의 CA 문제로 프로젝트 전반과 동일하게 verify=False 사용.
            resp = httpx.get(url, timeout=15, verify=False, follow_redirects=True)
            resp.raise_for_status()
            with open(path, "wb") as fp:
                fp.write(resp.content)
        except Exception:
            return None
    try:
        return Image.open(path).convert("RGB")
    except (OSError, IOError):
        return None


def _champion_splash(champion_name: str | None) -> Image.Image | None:
    if not champion_name:
        return None
    key = _champion_key_map().get(str(champion_name).strip())
    if not key:
        return None
    url = f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{key}_0.jpg"
    return _fetch_cached(url, f"splash_{key}.jpg")


# ── 그리기 헬퍼 ─────────────────────────────────────────────────────────
def _cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / img.width, height / img.height)
    resized = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _horizontal_gradient(width: int, height: int, color: tuple[int, int, int],
                         alpha_from: int, alpha_to: int, stop: float) -> Image.Image:
    """왼쪽 alpha_from → stop 지점에서 alpha_to 로 떨어지는 가로 그라데이션."""
    grad = Image.new("RGBA", (width, 1))
    stop_px = max(1, int(width * stop))
    px = []
    for x in range(width):
        if x < stop_px:
            a = int(alpha_from + (alpha_to - alpha_from) * (x / stop_px))
        else:
            a = alpha_to
        px.append((*color, a))
    grad.putdata(px)
    return grad.resize((width, height))


def _vertical_gradient(width: int, height: int, color: tuple[int, int, int],
                       alpha_top: int, alpha_bottom: int) -> Image.Image:
    grad = Image.new("RGBA", (1, height))
    px = [
        (*color, int(alpha_top + (alpha_bottom - alpha_top) * (y / max(1, height - 1))))
        for y in range(height)
    ]
    grad.putdata(px)
    return grad.resize((width, height))


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
          font: ImageFont.FreeTypeFont, *, fg, outline, fill=None, pad_x=14, pad_y=7) -> int:
    """둥근 배지를 그리고 다음 배지가 시작할 x 좌표를 반환한다."""
    x, y = xy
    tw = draw.textlength(text, font=font)
    box_h = font.size + pad_y * 2
    draw.rounded_rectangle(
        (x, y, x + tw + pad_x * 2, y + box_h),
        radius=box_h // 2, outline=outline, fill=fill, width=1,
    )
    draw.text((x + pad_x, y + pad_y - 1), text, font=font, fill=fg)
    return int(x + tw + pad_x * 2 + 10)


def _summary_value(report: dict, metric: str) -> str:
    for row in report.get("summary_table", []):
        if row.get("metric") == metric:
            return row.get("formatted_value") or "-"
    return "-"


# ── 카드 렌더링 ─────────────────────────────────────────────────────────
def render_style_card(report: dict[str, Any], player_name: str) -> bytes:
    styles = [s.strip() for s in str(report.get("player_type") or "").split("+")]
    primary = report.get("recommended_style") or (styles[0] if styles else "한타지향형")
    secondary = next((s for s in styles if s and s != primary), None)
    meta = STYLE_META.get(primary, {"emoji": "🎮", "accent": DEFAULT_ACCENT, "tagline": ""})
    accent = meta["accent"]
    position_label = report.get("main_position_label") or "미정"

    best_champ = (report.get("champion_analysis") or {}).get("best")

    # ── 배경 ─────────────────────────────────────────────────────────
    card = Image.new("RGB", (W, H), (11, 17, 32))
    splash = _champion_splash(best_champ.get("champion_name")) if best_champ else None
    if splash:
        bg = _cover_crop(splash, W, H)
        card.paste(bg, (0, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_horizontal_gradient(W, H, (10, 15, 28), 248, 30, 0.74))
    overlay.alpha_composite(_vertical_gradient(W, H, (8, 12, 24), 0, 190), (0, 0))
    # 헤더(날짜 등) 가독성을 위한 상단 살짝 어둡게.
    overlay.alpha_composite(_vertical_gradient(W, 110, (8, 12, 24), 150, 0), (0, 0))
    # 베이스를 RGB로 확정해야 ImageDraw 의 "RGBA" 잉크 블렌딩이 동작한다
    # (RGBA 베이스에서는 반투명 잉크가 블렌딩되지 않고 픽셀에 그대로 덮어써진다).
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")

    # 상단 강조 라인 + 카드 테두리
    draw.rectangle((0, 0, W, 5), fill=(*accent, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(255, 255, 255, 36), width=1)

    white = (255, 255, 255, 255)
    gray = (176, 190, 210, 255)
    dim = (150, 162, 182, 255)

    x = MARGIN
    today = datetime.now(KST).strftime("%Y.%m.%d")

    # ── 헤더 ─────────────────────────────────────────────────────────
    header_font = _font("SemiBold", 20)
    draw.text((x, 42), "G O L  L E A G U E  ·  P L A Y E R  C A R D", font=header_font, fill=(*accent, 255))
    date_font = _font("Regular", 18)
    date_text = f"{today} 기준 · {report.get('matches', 0)}판 분석"
    draw.text((W - MARGIN - draw.textlength(date_text, font=date_font), 44), date_text, font=date_font, fill=dim)

    # ── 플레이어 이름 ────────────────────────────────────────────────
    name_size = 68 if len(player_name) <= 7 else 54
    name_font = _font("Heavy", name_size)
    y = 90
    draw.text((x - 2, y), player_name, font=name_font, fill=white)
    y += name_size + 20

    # ── 배지 라인 (포지션 · 전적) ─────────────────────────────────────
    badge_font = _font("SemiBold", 21)
    bx = x
    bx = _pill(draw, (bx, y), f"주 포지션 · {position_label}", badge_font,
               fg=white, outline=(255, 255, 255, 90))
    sub_label = report.get("sub_position_label")
    if sub_label and report.get("sub_position") not in (None, "UNKNOWN"):
        bx = _pill(draw, (bx, y), f"부 · {sub_label}", badge_font, fg=gray, outline=(255, 255, 255, 55))
    matches = int(report.get("matches") or 0)
    wr_row = next((r for r in report.get("summary_table", []) if r.get("metric") == "win_rate"), None)
    if wr_row and matches:
        wins = round(matches * float(wr_row.get("value") or 0) / 100)
        bx = _pill(draw, (bx, y), f"{wins}승 {matches - wins}패", badge_font,
                   fg=(*accent, 255), outline=(*accent, 150))
    y += 35 + 24

    # ── 스타일 타입 ──────────────────────────────────────────────────
    style_font = _font("ExtraBold", 56)
    emoji_img = _emoji_image(meta["emoji"], 52)
    ex = x
    if emoji_img:
        card.paste(emoji_img, (ex, y + 6), emoji_img)
        ex += emoji_img.width + 16
        draw = ImageDraw.Draw(card, "RGBA")
    draw.text((ex, y), primary, font=style_font, fill=(*accent, 255))
    if secondary:
        sub_font = _font("SemiBold", 24)
        style_w = draw.textlength(primary, font=style_font)
        draw.text((ex + style_w + 14, y + 28), f"+ {secondary}", font=sub_font, fill=gray)
    y += 56 + 20

    # ── 스타일 태그라인 ──────────────────────────────────────────────
    tagline = meta.get("tagline")
    if tagline:
        tagline_font = _font("Medium", 24)
        tag_lines = _wrap_text(draw, f"“{tagline}”", tagline_font, PANEL_WIDTH - 20)
        for line in tag_lines[:2]:
            draw.text((x, y), line, font=tagline_font, fill=(201, 211, 224, 255))
            y += 34

    # ── 구분선 ───────────────────────────────────────────────────────
    y += 16
    draw.line((x, y, x + PANEL_WIDTH - 40, y), fill=(255, 255, 255, 60), width=1)
    y += 18

    # ── 지표 4종 ─────────────────────────────────────────────────────
    stats = [
        ("승률", _summary_value(report, "win_rate")),
        ("KDA", _summary_value(report, "kda")),
        ("DPM", _summary_value(report, "dpm")),
        ("킬관여", _summary_value(report, "kill_participation")),
    ]
    value_font = _font("ExtraBold", 37)
    stat_label_font = _font("Regular", 18)
    col_w = (PANEL_WIDTH - 40) // 4
    for i, (label, value) in enumerate(stats):
        cx = x + i * col_w
        draw.text((cx, y), value, font=value_font, fill=white)
        draw.text((cx, y + 46), label, font=stat_label_font, fill=dim)
    y += 82

    # ── 강점 칩 ──────────────────────────────────────────────────────
    strengths = [s.get("label") for s in report.get("strengths_top3", []) if s.get("label")]
    if strengths:
        chip_font = _font("SemiBold", 20)
        draw.text((x, y + 8), "강점", font=stat_label_font, fill=dim)
        cx = x + 58
        for label in strengths[:3]:
            cx = _pill(draw, (cx, y), label, chip_font,
                       fg=(*accent, 255), outline=(*accent, 140), fill=(*accent, 36))
        y += 48

    # ── 최근 폼 ──────────────────────────────────────────────────────
    form = report.get("recent_form")
    if form and form.get("last5") and y + 32 <= H - 44:
        draw.text((x, y + 6), "최근", font=stat_label_font, fill=dim)
        dot_x = x + 58
        dot_font = _font("Bold", 16)
        for result in form["last5"]:
            win = result == "승"
            color = (74, 222, 128) if win else (248, 113, 113)
            draw.ellipse((dot_x, y, dot_x + 30, y + 30), fill=(*color, 235))
            ch = "승" if win else "패"
            tw = draw.textlength(ch, font=dot_font)
            draw.text((dot_x + (30 - tw) / 2, y + 5), ch, font=dot_font, fill=(15, 23, 42, 255))
            dot_x += 38
        tag = form.get("tag")
        if tag:
            draw.text((dot_x + 8, y + 4), tag, font=_font("SemiBold", 20), fill=gray)

    # ── 우하단 시그니처 챔피언 플레이트 ───────────────────────────────
    if best_champ:
        plate_font = _font("SemiBold", 21)
        plate_text = (
            f"시그니처  {best_champ['champion_name']}"
            f"  ·  {best_champ['games']}판 승률 {best_champ['win_rate']:.0f}%"
        )
        tw = draw.textlength(plate_text, font=plate_font)
        px2 = W - MARGIN - tw - 28
        py2 = H - 96
        draw.rounded_rectangle(
            (px2 - 14, py2 - 10, px2 + tw + 14, py2 + 31 + 10),
            radius=14, fill=(8, 12, 24, 175), outline=(255, 255, 255, 45), width=1,
        )
        draw.text((px2, py2 + 3), plate_text, font=plate_font, fill=white)

    # ── 푸터 ─────────────────────────────────────────────────────────
    footer_font = _font("Regular", 17)
    footer = f"GOL 내전 데이터 기반 · {public_host()}"
    draw.text((W - MARGIN - draw.textlength(footer, font=footer_font), H - 40),
              footer, font=footer_font, fill=dim)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()
