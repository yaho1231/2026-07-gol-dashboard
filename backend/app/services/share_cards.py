"""공유용 카드 이미지 3종 — 경기 흐름 그래프 · 명예의 전당 · 듀오 시너지.

/경기흐름, /명예의전당, /듀오카드 명령이 첨부 이미지로 올린다.
폰트·스플래시 캐시·그라데이션 헬퍼는 style_card 의 것을 그대로 재사용한다.

Pillow 함정: 베이스를 RGB로 확정한 뒤 ImageDraw.Draw(img, "RGBA") 를 써야
반투명 잉크가 블렌딩된다(RGBA 베이스에서는 픽셀에 그대로 덮어써진다).
"""

from __future__ import annotations

import io
import json
from collections import Counter
from datetime import datetime
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from app.config import public_host
from app.models.match import Match, MatchParticipant
from app.models.player import Player
from app.services import records_service
from app.services.champion_data import (
    get_champion_image_key,
    get_latest_version,
    resolve_champion_name,
)
from app.services.match_report import MIN_VALID_DURATION
from app.services.position_service import normalize_position, position_label
from app.services.style_card import (
    KST,
    MARGIN,
    H,
    W,
    _champion_splash,
    _cover_crop,
    _emoji_image,
    _fetch_cached,
    _font,
    _horizontal_gradient,
    _pill,
    _vertical_gradient,
)

WHITE = (255, 255, 255, 255)
GRAY = (176, 190, 210, 255)
DIM = (150, 162, 182, 255)
GOLD = (222, 186, 88)
BLUE = (96, 158, 255)
RED = (248, 113, 113)
BG = (11, 17, 32)

FOOTER = f"GOL 내전 데이터 기반 · {public_host()}"


def _footer(draw: ImageDraw.ImageDraw) -> None:
    font = _font("Regular", 17)
    draw.text((W - MARGIN - draw.textlength(FOOTER, font=font), H - 40),
              FOOTER, font=font, fill=DIM)


def _center_text(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str,
                 font, fill) -> None:
    draw.text((cx - draw.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def _png(card: Image.Image) -> bytes:
    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════
# 1) 경기 흐름(모멘텀) 그래프 카드
# ═══════════════════════════════════════════════════════════════════════
def _load_momentum_match(db: Session, match_id: str | None = None):
    """타임라인(concat_seq_dict)이 있는 가장 최근 유효 경기와 시계열을 찾는다."""
    q = db.query(Match).filter(Match.game_duration >= MIN_VALID_DURATION)
    if match_id:
        q = q.filter(Match.match_id == match_id)
    for m in q.order_by(Match.game_creation.desc()).limit(15).all():
        try:
            raw = json.loads(m.raw_data or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        ta = raw.get("time_analysis") or {}
        seq = ta.get("concat_seq_dict") or {}
        series: list[tuple[int, float]] = []
        for k in sorted(seq, key=lambda x: int(x) if str(x).isdigit() else -1):
            if not str(k).isdigit():
                continue
            v = seq[k]
            if isinstance(v, dict) and "B_Avg" in v:
                try:
                    series.append((int(k), float(v["B_Avg"])))
                except (TypeError, ValueError):
                    pass
        if len(series) >= 8:
            return m, ta, series
    return None


def _collect_events(ta: dict) -> tuple[list[tuple[float, str, str]], list[tuple[float, str]]]:
    """(오브젝트 이벤트, 킬 이벤트) — 각각 (분, 진영[, 라벨])."""
    objectives: list[tuple[float, str, str]] = []
    kills: list[tuple[float, str]] = []
    for evs in (ta.get("concat_events_dict") or {}).values():
        if not isinstance(evs, dict):
            continue
        for item in evs.values():
            e = (item or {}).get("event") or {}
            side, mf = e.get("side"), e.get("minute_float")
            if mf is None or side not in ("BLUE", "RED"):
                continue
            etype = e.get("type")
            if etype == "Champion":
                kills.append((float(mf), side))
            elif etype == "Monster":
                target = str(e.get("target") or "")
                if "BARON" in target:
                    label = "바론"
                elif "HERALD" in target:
                    label = "전령"
                elif "DRAGON" in target:
                    label = "용"
                else:
                    label = ""
                if label:
                    objectives.append((float(mf), side, label))
    return objectives, kills


def render_momentum_card(db: Session, match_id: str | None = None) -> bytes | None:
    loaded = _load_momentum_match(db, match_id)
    if not loaded:
        return None
    m, ta, series = loaded

    rows = (
        db.query(MatchParticipant, Player.display_name)
        .outerjoin(Player, Player.id == MatchParticipant.player_id)
        .filter(MatchParticipant.match_id == m.match_id)
        .all()
    )
    teams: dict[int, list[dict[str, Any]]] = {100: [], 200: []}
    for p, display_name in rows:
        teams.setdefault(p.team_id, []).append({
            "name": display_name or p.summoner_name,
            "champion": resolve_champion_name(p.champion_id, p.champion_name),
            "kills": int(p.kills or 0),
            "win": bool(p.win),
            "ai_score": float(p.ai_score or 0),
            "raw_data": p.raw_data,
        })
    blue, red = teams.get(100, []), teams.get(200, [])
    blue_win = any(p["win"] for p in blue)
    blue_kills = sum(p["kills"] for p in blue)
    red_kills = sum(p["kills"] for p in red)
    accent = BLUE if blue_win else RED

    mvp = None
    flagged = next(
        (p for p in blue + red if records_service._parse_flags(p["raw_data"])["mvp"]), None)
    if flagged:
        mvp = flagged
    else:
        winners = [p for p in (blue if blue_win else red)]
        if winners:
            best = max(winners, key=lambda p: p["ai_score"])
            mvp = best if best["ai_score"] > 0 else None

    objectives, kills = _collect_events(ta)

    # ── 배경 ─────────────────────────────────────────────────────────
    card = Image.new("RGB", (W, H), BG)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_vertical_gradient(W, H, (16, 24, 44), 160, 0))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W, 5), fill=(*accent, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(255, 255, 255, 36), width=1)

    x = MARGIN
    # ── 헤더 ─────────────────────────────────────────────────────────
    draw.text((x, 40), "M A T C H  F L O W  ·  경 기  흐 름", font=_font("SemiBold", 20),
              fill=(*accent, 255))
    date_font = _font("Regular", 18)
    played = datetime.fromtimestamp((m.game_creation or 0) / 1000, tz=KST)
    date_text = f"{played.strftime('%Y.%m.%d %H:%M')} · {int((m.game_duration or 0) / 60)}분"
    draw.text((W - MARGIN - draw.textlength(date_text, font=date_font), 42),
              date_text, font=date_font, fill=DIM)

    # ── 스코어 라인 ──────────────────────────────────────────────────
    y = 82
    score_font = _font("Heavy", 52)
    seg = [
        ("블루", BLUE), (f"  {blue_kills}", WHITE[:3]), ("  :  ", DIM[:3]),
        (f"{red_kills}  ", WHITE[:3]), ("레드", RED),
    ]
    sx = x
    for text, color in seg:
        draw.text((sx, y), text, font=score_font, fill=(*color, 255))
        sx += draw.textlength(text, font=score_font)
    badge_font = _font("Bold", 22)
    _pill(draw, (int(sx) + 20, y + 18), f"{'블루' if blue_win else '레드'}팀 승리", badge_font,
          fg=(*accent, 255), outline=(*accent, 160), fill=(*accent, 40))
    if mvp:
        mvp_text = f"👑 MVP {mvp['name']} ({mvp['champion']})"
        mvp_font = _font("SemiBold", 21)
        emoji = _emoji_image("👑", 22)
        tw = draw.textlength(mvp_text[2:], font=mvp_font)
        mx = W - MARGIN - tw - (26 if emoji else 0)
        if emoji:
            card.paste(emoji, (int(mx), y + 26), emoji)
            draw = ImageDraw.Draw(card, "RGBA")
        draw.text((mx + (26 if emoji else 0), y + 26), mvp_text[2:], font=mvp_font, fill=GRAY)

    # ── 로스터 ───────────────────────────────────────────────────────
    y = 158
    roster_font = _font("Regular", 18)
    for team, color in ((blue, BLUE), (red, RED)):
        if not team:
            continue
        draw.ellipse((x, y + 5, x + 12, y + 17), fill=(*color, 255))
        names = " · ".join(f"{p['name']}({p['champion']})" for p in team)
        draw.text((x + 22, y), names[:110], font=roster_font, fill=GRAY)
        y += 30

    # ── 그래프 영역 ──────────────────────────────────────────────────
    gx0, gx1 = x, W - MARGIN
    gy0, gy1 = 268, 566
    vals = [v for _, v in series]
    v_min, v_max = min(min(vals), 44.0) - 3, max(max(vals), 56.0) + 3
    last_min = series[-1][0]

    def to_x(minute: float) -> float:
        return gx0 + (min(minute, last_min) / max(1, last_min)) * (gx1 - gx0)

    def to_y(val: float) -> float:
        return gy1 - (val - v_min) / (v_max - v_min) * (gy1 - gy0)

    # 세로 눈금(5분 간격) + 50% 기준선
    tick_font = _font("Regular", 15)
    for minute in range(0, last_min + 1, 5):
        tx = to_x(minute)
        draw.line((tx, gy0, tx, gy1), fill=(255, 255, 255, 18), width=1)
        _center_text(draw, int(tx), gy1 + 10, f"{minute}분", tick_font, DIM)
    mid_y = to_y(50)
    for dash_x in range(gx0, gx1, 14):
        draw.line((dash_x, mid_y, dash_x + 7, mid_y), fill=(255, 255, 255, 70), width=1)

    # 우세 영역 채우기 — 50 기준 위(블루)/아래(레드)를 클램프한 폴리곤으로 채운다.
    pts = [(to_x(mn), v) for mn, v in series]
    blue_poly = [(px, to_y(max(v, 50))) for px, v in pts]
    red_poly = [(px, to_y(min(v, 50))) for px, v in pts]
    draw.polygon(blue_poly + [(pts[-1][0], mid_y), (pts[0][0], mid_y)], fill=(*BLUE, 46))
    draw.polygon(red_poly + [(pts[-1][0], mid_y), (pts[0][0], mid_y)], fill=(*RED, 46))

    # 본선 — 넓은 글로우 위에 본선을 겹쳐 그린다.
    line_pts = [(px, to_y(v)) for px, v in pts]
    draw.line(line_pts, fill=(255, 255, 255, 60), width=7, joint="curve")
    draw.line(line_pts, fill=(235, 242, 255, 255), width=3, joint="curve")

    label_font = _font("SemiBold", 17)
    draw.text((gx0 + 8, gy0 + 6), "▲ 블루 우세", font=label_font, fill=(*BLUE, 220))
    draw.text((gx0 + 8, gy1 - 28), "▼ 레드 우세", font=label_font, fill=(*RED, 220))

    def interp(minute: float) -> float:
        for (m0, v0), (m1, v1) in zip(series, series[1:]):
            if m0 <= minute <= m1:
                t = (minute - m0) / max(1e-6, m1 - m0)
                return v0 + (v1 - v0) * t
        return series[-1][1]

    # 오브젝트 마커 — 블루는 라벨을 위로, 레드는 아래로 띄워 겹침을 줄인다.
    obj_font = _font("Bold", 16)
    for mf, side, label in objectives:
        color = BLUE if side == "BLUE" else RED
        px, py = to_x(mf), to_y(interp(mf))
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(*color, 255),
                     outline=(255, 255, 255, 200), width=2)
        ly = py - 30 if side == "BLUE" else py + 14
        _center_text(draw, int(px), int(ly), label, obj_font, (*color, 235))

    # 킬 틱 — 그래프 하단 밴드에 진영색 세로선.
    for mf, side in kills:
        color = BLUE if side == "BLUE" else RED
        px = to_x(mf)
        draw.line((px, gy1 + 34, px, gy1 + 46), fill=(*color, 190), width=2)
    kill_label = "킬 발생 지점"
    draw.text((gx1 - draw.textlength(kill_label, font=tick_font), gy1 + 52),
              kill_label, font=tick_font, fill=DIM)

    # ── 한 줄 관전평 ─────────────────────────────────────────────────
    # time_analysis 승률값은 50 근처로 압축돼 있어 절대값 대신 상대 임계를 쓴다.
    lo, hi = min(vals), max(vals)
    if blue_win and lo <= 43:
        verdict = "🔥 블루팀, 열세를 뒤집은 짜릿한 역전승!"
    elif (not blue_win) and hi >= 57:
        verdict = "🔥 레드팀, 끌려가다 터뜨린 대역전승!"
    elif blue_win and lo >= 47:
        verdict = "🛡️ 블루팀, 흔들림 없는 리드 굳히기 승리"
    elif (not blue_win) and hi <= 53:
        verdict = "🛡️ 레드팀, 시종일관 주도권을 쥔 승리"
    else:
        verdict = "⚔️ 엎치락뒤치락, 마지막까지 팽팽했던 접전"
    verdict_font = _font("SemiBold", 22)
    emoji_char, verdict_text = verdict[:1], verdict[2:]
    emoji = _emoji_image(emoji_char, 24)
    vx = x
    if emoji:
        card.paste(emoji, (vx, H - 66), emoji)
        draw = ImageDraw.Draw(card, "RGBA")
        vx += 32
    draw.text((vx, H - 64), verdict_text, font=verdict_font, fill=WHITE)

    _footer(draw)
    return _png(card)


# ═══════════════════════════════════════════════════════════════════════
# 2) 명예의 전당 카드
# ═══════════════════════════════════════════════════════════════════════
def render_hall_of_fame_card(db: Session) -> bytes | None:
    data = records_service.build_hall_of_fame(db)
    records = data.get("records") or []
    boards = data.get("boards") or []
    if not records and not boards:
        return None

    # ── 배경 — 다크 네이비 + 우측에 대표 기록 챔피언 스플래시를 은은하게 ──
    card = Image.new("RGB", (W, H), (13, 15, 24))
    splash_champ = next((r["champion"] for r in records if r.get("champion")), None)
    splash = _champion_splash(splash_champ) if splash_champ else None
    if splash:
        bg = _cover_crop(splash, W, H)
        card.paste(bg, (0, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_horizontal_gradient(W, H, (13, 15, 24), 252, 168, 0.65))
    overlay.alpha_composite(_vertical_gradient(W, H, (10, 11, 18), 60, 210))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W, 5), fill=(*GOLD, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(*GOLD, 70), width=1)

    x = MARGIN
    # ── 헤더 ─────────────────────────────────────────────────────────
    draw.text((x, 40), "H A L L  O F  F A M E", font=_font("SemiBold", 20), fill=(*GOLD, 255))
    title_font = _font("Heavy", 52)
    tx = x
    emoji = _emoji_image("🏛️", 50)
    if emoji:
        card.paste(emoji, (tx, 74), emoji)
        draw = ImageDraw.Draw(card, "RGBA")
        tx += emoji.width + 18
    draw.text((tx, 68), "GOL 명예의 전당", font=title_font, fill=WHITE)
    sub_font = _font("Regular", 18)
    today = datetime.now(KST).strftime("%Y.%m.%d")
    sub = f"유효 경기 {data.get('total_matches', 0)}판 누적 · {today} 기준"
    draw.text((W - MARGIN - draw.textlength(sub, font=sub_font), 44), sub, font=sub_font, fill=DIM)

    # ── 왼쪽 — 단일 경기 최고 기록 ────────────────────────────────────
    y = 168
    section_font = _font("SemiBold", 20)
    draw.text((x, y), "단일 경기 최고 기록", font=section_font, fill=(*GOLD, 255))
    draw.line((x, y + 32, x + 540, y + 32), fill=(*GOLD, 80), width=1)
    y += 46

    label_font = _font("SemiBold", 22)
    value_font = _font("ExtraBold", 24)
    holder_font = _font("Regular", 17)
    for rec in records[:6]:
        emoji = _emoji_image(rec["emoji"], 26)
        if emoji:
            card.paste(emoji, (x, y + 2), emoji)
            draw = ImageDraw.Draw(card, "RGBA")
        draw.text((x + 38, y), rec["label"], font=label_font, fill=WHITE)
        draw.text((x + 540 - draw.textlength(rec["value_text"], font=value_font), y),
                  rec["value_text"], font=value_font, fill=(*GOLD, 255))
        holder = f"{rec['name']} ({rec['champion']}) · {rec['date_text']}"
        draw.text((x + 38, y + 30), holder, font=holder_font, fill=DIM)
        y += 64

    # ── 오른쪽 — 누적 보드 2×2 ───────────────────────────────────────
    wanted = ["누적 MVP", "통산 다승", "최다 연승", "최다 출전"]
    grid = [b for title in wanted for b in boards if b["title"] == title]
    cells = [(672, 168), (952, 168), (672, 372), (952, 372)]
    board_title_font = _font("SemiBold", 20)
    line_font = _font("Regular", 19)
    rank_colors = [(*GOLD, 255), (198, 208, 224, 255), (205, 148, 96, 255)]
    for (cx, cy), board in zip(cells, grid):
        emoji = _emoji_image(board["emoji"], 20)
        bx = cx
        if emoji:
            card.paste(emoji, (bx, cy + 1), emoji)
            draw = ImageDraw.Draw(card, "RGBA")
            bx += 28
        draw.text((bx, cy), board["title"], font=board_title_font, fill=(*GOLD, 255))
        draw.line((cx, cy + 30, cx + 216, cy + 30), fill=(255, 255, 255, 40), width=1)
        ly = cy + 42
        for i, line in enumerate(board["lines"][:3]):
            # 보드 라인은 봇 임베드용 마크다운("🥇 **이름** — 9회") — 카드용으로 파싱한다.
            body = line.split(" ", 1)[-1].replace("**", "")
            draw.text((cx, ly), f"{i + 1}", font=_font("Bold", 19), fill=rank_colors[i])
            draw.text((cx + 24, ly), body[:22], font=line_font, fill=GRAY if i else WHITE)
            ly += 32

    _footer(draw)
    return _png(card)


# ═══════════════════════════════════════════════════════════════════════
# 3) 듀오 시너지 카드
# ═══════════════════════════════════════════════════════════════════════
_DUO_VERDICTS = [
    (70, "💎", "천생연분 듀오", "상대팀에게 애도를 표합니다"),
    (60, "🔥", "합이 잘 맞는 듀오", "같이 큐 돌리면 승률이 오릅니다"),
    (50, "🤝", "무난한 케미", "안정적으로 제 몫을 하는 조합"),
    (40, "🌱", "성장 중인 듀오", "서로에게 적응할 시간이 필요합니다"),
    (0, "🧊", "환상의 (반대) 케미", "…따로 큐가 서로에게 좋을지도?"),
]


def render_duo_card(db: Session, player_a_id: int, player_b_id: int) -> bytes:
    """두 플레이어의 같은 팀 성적 카드. 표본 부족 등은 ValueError(한국어 메시지)."""
    if player_a_id == player_b_id:
        raise ValueError("서로 다른 두 플레이어를 지정해주세요.")
    players = {p.id: p for p in db.query(Player).filter(Player.id.in_([player_a_id, player_b_id]))}
    if len(players) < 2:
        raise ValueError("등록되지 않은 플레이어가 있습니다.")

    rows = (
        db.query(MatchParticipant, Match.game_creation)
        .join(Match, Match.match_id == MatchParticipant.match_id)
        .filter(
            MatchParticipant.player_id.in_([player_a_id, player_b_id]),
            Match.game_duration >= MIN_VALID_DURATION,
        )
        .all()
    )
    by_match: dict[str, dict[int, Any]] = {}
    creation: dict[str, int] = {}
    for p, game_creation in rows:
        by_match.setdefault(p.match_id, {})[int(p.player_id)] = p
        creation[p.match_id] = int(game_creation or 0)

    together: list[tuple[int, Any, Any]] = []
    versus_games = versus_a_wins = 0
    for mid, pair in by_match.items():
        if len(pair) < 2:
            continue
        pa, pb = pair[player_a_id], pair[player_b_id]
        if pa.team_id == pb.team_id:
            together.append((creation[mid], pa, pb))
        else:
            versus_games += 1
            versus_a_wins += 1 if pa.win else 0

    if len(together) < 2:
        raise ValueError("두 플레이어가 같은 팀으로 뛴 경기가 2판 미만입니다.")

    together.sort(key=lambda t: t[0])
    games = len(together)
    wins = sum(1 for _, pa, _ in together if pa.win)
    wr = round(wins / games * 100)
    last5 = ["승" if pa.win else "패" for _, pa, _ in together[-5:]]

    def _main_champ(idx: int) -> str | None:
        counts = Counter(
            resolve_champion_name(t[idx].champion_id, t[idx].champion_name) for t in together)
        return counts.most_common(1)[0][0] if counts else None

    champ_a, champ_b = _main_champ(1), _main_champ(2)
    name_a = players[player_a_id].display_name or f"Player {player_a_id}"
    name_b = players[player_b_id].display_name or f"Player {player_b_id}"

    threshold, v_emoji, v_label, v_tagline = next(t for t in _DUO_VERDICTS if wr >= t[0])
    accent = GOLD if wr >= 50 else (140, 165, 200)

    # ── 배경 — 좌우 반반 스플래시 + 중앙 어두운 밴드 ─────────────────
    card = Image.new("RGB", (W, H), BG)
    half = W // 2
    for champ, box_x in ((champ_a, 0), (champ_b, half)):
        splash = _champion_splash(champ) if champ else None
        if splash:
            card.paste(_cover_crop(splash, half, H), (box_x, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_vertical_gradient(W, H, (8, 12, 24), 40, 200))
    # 중앙 밴드 — 수치 텍스트 가독성 확보용.
    band = _horizontal_gradient(470, H, (8, 12, 24), 0, 225, 0.55)
    overlay.alpha_composite(band, (half - 470, 0))
    overlay.alpha_composite(band.transpose(Image.FLIP_LEFT_RIGHT), (half, 0))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W, 5), fill=(*accent, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(255, 255, 255, 36), width=1)
    draw.line((half, 24, half, 120), fill=(*accent, 90), width=1)
    draw.line((half, H - 120, half, H - 24), fill=(*accent, 90), width=1)

    # ── 헤더 + 이름 ──────────────────────────────────────────────────
    _center_text(draw, half, 40, "D U O  S Y N E R G Y", _font("SemiBold", 20), (*accent, 255))

    name_font_size = 54
    name_font = _font("Heavy", name_font_size)
    champ_font = _font("Regular", 19)
    draw.text((MARGIN, 96), name_a, font=name_font, fill=WHITE)
    if champ_a:
        draw.text((MARGIN, 96 + name_font_size + 12), f"주력 {champ_a}", font=champ_font, fill=GRAY)
    bw = draw.textlength(name_b, font=name_font)
    draw.text((W - MARGIN - bw, 96), name_b, font=name_font, fill=WHITE)
    if champ_b:
        cw = draw.textlength(f"주력 {champ_b}", font=champ_font)
        draw.text((W - MARGIN - cw, 96 + name_font_size + 12), f"주력 {champ_b}",
                  font=champ_font, fill=GRAY)
    _center_text(draw, half, 108, "VS", _font("ExtraBold", 34), (*accent, 220))

    # ── 중앙 — 함께 승률 ─────────────────────────────────────────────
    big_font = _font("Heavy", 110)
    _center_text(draw, half, 226, f"{wr}%", big_font, (*accent, 255))
    _center_text(draw, half, 352, f"함께 {games}판  ·  {wins}승 {games - wins}패",
                 _font("SemiBold", 26), WHITE)

    # 판정 배지 + 태그라인
    v_font = _font("Bold", 24)
    v_text = f"{v_label}"
    emoji = _emoji_image(v_emoji, 26)
    tw = draw.textlength(v_text, font=v_font) + (36 if emoji else 0)
    vx = int(half - tw / 2)
    if emoji:
        card.paste(emoji, (vx, 404), emoji)
        draw = ImageDraw.Draw(card, "RGBA")
        vx += 36
    draw.text((vx, 402), v_text, font=v_font, fill=(*accent, 255))
    _center_text(draw, half, 442, f"“{v_tagline}”", _font("Medium", 21), (201, 211, 224, 255))

    # ── 최근 함께한 5판 ──────────────────────────────────────────────
    dot_font = _font("Bold", 16)
    dot_x = half - (len(last5) * 38 - 8) // 2
    for result in last5:
        win = result == "승"
        color = (74, 222, 128) if win else (248, 113, 113)
        draw.ellipse((dot_x, 492, dot_x + 30, 522), fill=(*color, 235))
        ch_w = draw.textlength(result, font=dot_font)
        draw.text((dot_x + (30 - ch_w) / 2, 497), result, font=dot_font, fill=(15, 23, 42, 255))
        dot_x += 38
    _center_text(draw, half, 532, "최근 함께한 경기", _font("Regular", 16), DIM)

    # ── 맞대결 전적(있을 때만) ───────────────────────────────────────
    if versus_games:
        h2h = (f"맞대결 {versus_games}판 — {name_a} {versus_a_wins} : "
               f"{versus_games - versus_a_wins} {name_b}")
        _center_text(draw, half, 576, h2h, _font("SemiBold", 20), GRAY)

    _footer(draw)
    return _png(card)


# ═══════════════════════════════════════════════════════════════════════
# 4) 팀생성 로스터 카드
# ═══════════════════════════════════════════════════════════════════════
def _champion_icon(champion_name: str | None, size: int) -> Image.Image | None:
    """챔피언 정사각 아이콘(DDragon). 실패 시 None."""
    if not champion_name:
        return None
    key = get_champion_image_key(champion_name=str(champion_name).strip())
    if not key:
        return None
    version = get_latest_version()
    url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{key}.png"
    icon = _fetch_cached(url, f"icon_{key}.png")
    return icon.resize((size, size), Image.LANCZOS) if icon else None


def _player_profiles(db: Session, player_ids: list[int]) -> dict[int, dict[str, Any]]:
    """로스터 카드용 플레이어별 전적 요약 — 판수·승률·주 포지션·주 챔피언."""
    rows = (
        db.query(MatchParticipant)
        .join(Match, Match.match_id == MatchParticipant.match_id)
        .filter(
            MatchParticipant.player_id.in_(player_ids),
            Match.game_duration >= MIN_VALID_DURATION,
        )
        .all()
    )
    acc: dict[int, dict[str, Any]] = {
        pid: {"games": 0, "wins": 0, "positions": Counter(), "champs": Counter()}
        for pid in player_ids
    }
    for p in rows:
        a = acc[int(p.player_id)]
        a["games"] += 1
        a["wins"] += 1 if p.win else 0
        pos = normalize_position(p.position)
        if pos != "UNKNOWN":
            a["positions"][pos] += 1
        a["champs"][resolve_champion_name(p.champion_id, p.champion_name)] += 1

    profiles: dict[int, dict[str, Any]] = {}
    for pid, a in acc.items():
        games = a["games"]
        profiles[pid] = {
            "games": games,
            "win_rate": round(a["wins"] / games * 100) if games else None,
            "position": position_label(a["positions"].most_common(1)[0][0]) if a["positions"] else None,
            "champion": a["champs"].most_common(1)[0][0] if a["champs"] else None,
        }
    return profiles


def render_roster_card(db: Session, payload: dict[str, Any]) -> bytes:
    """매치메이킹 결과(blue_team/red_team/mmr_difference)를 로스터 배너로 렌더링한다."""
    blue = payload.get("blue_team") or {}
    red = payload.get("red_team") or {}
    blue_players = blue.get("players") or []
    red_players = red.get("players") or []
    ids = [int(p["id"]) for p in blue_players + red_players if p.get("id") is not None]
    profiles = _player_profiles(db, ids) if ids else {}

    card = Image.new("RGB", (W, H), BG)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_vertical_gradient(W, H, (16, 24, 44), 150, 0))
    # 좌우 진영 톤 — 블루/레드 기운을 옅게 깔아준다.
    overlay.alpha_composite(_horizontal_gradient(W // 2, H, BLUE, 26, 0, 1.0))
    overlay.alpha_composite(
        _horizontal_gradient(W // 2, H, RED, 26, 0, 1.0).transpose(Image.FLIP_LEFT_RIGHT),
        (W // 2, 0),
    )
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W // 2, 5), fill=(*BLUE, 255))
    draw.rectangle((W // 2, 0, W, 5), fill=(*RED, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(255, 255, 255, 36), width=1)

    x = MARGIN
    half = W // 2
    draw.text((x, 40), "T E A M  M A T C H U P", font=_font("SemiBold", 20), fill=(222, 230, 245, 255))
    date_font = _font("Regular", 18)
    mode_labels = {"mmr": "평균 MMR", "tier": "티어", "lane": "주 라인"}
    mode = mode_labels.get(str(payload.get("balance_mode") or "mmr"), "평균 MMR")
    sub = f"{datetime.now(KST).strftime('%Y.%m.%d')} · {mode} 밸런싱"
    draw.text((W - MARGIN - draw.textlength(sub, font=date_font), 42), sub, font=date_font, fill=DIM)

    # 팀 헤더
    team_font = _font("Heavy", 40)
    avg_font = _font("SemiBold", 20)
    draw.text((x, 78), "블루팀", font=team_font, fill=(*BLUE, 255))
    draw.text((x, 128), f"평균 MMR {blue.get('avg_mmr', 0):.0f}", font=avg_font, fill=GRAY)
    red_title_w = draw.textlength("레드팀", font=team_font)
    draw.text((W - MARGIN - red_title_w, 78), "레드팀", font=team_font, fill=(*RED, 255))
    red_avg = f"평균 MMR {red.get('avg_mmr', 0):.0f}"
    draw.text((W - MARGIN - draw.textlength(red_avg, font=avg_font), 128), red_avg,
              font=avg_font, fill=GRAY)
    _center_text(draw, half, 92, "VS", _font("ExtraBold", 44), (222, 230, 245, 230))

    # ── 플레이어 행 5줄 × 2열 ────────────────────────────────────────
    row_y0, row_h, icon_size = 178, 74, 52
    name_font = _font("Bold", 25)
    sub_font = _font("Regular", 17)
    mmr_font = _font("ExtraBold", 22)
    col_w = half - MARGIN - 60

    def _draw_side(players: list[dict], left: bool, color: tuple[int, int, int]) -> None:
        for i, p in enumerate(players[:5]):
            ry = row_y0 + i * row_h
            rx = x if left else half + 60
            prof = profiles.get(int(p.get("id") or -1), {})
            icon = _champion_icon(prof.get("champion"), icon_size)
            if icon:
                card.paste(icon, (rx, ry))
                inner = ImageDraw.Draw(card, "RGBA")
                inner.rectangle((rx, ry, rx + icon_size, ry + icon_size),
                                outline=(*color, 160), width=2)
            else:
                draw.rounded_rectangle((rx, ry, rx + icon_size, ry + icon_size), radius=8,
                                       outline=(*color, 120), width=2)
                initial = str(p.get("display_name") or "?")[:1]
                _center_text(draw, rx + icon_size // 2, ry + 12, initial, _font("Bold", 24), GRAY)
            tx = rx + icon_size + 16
            draw.text((tx, ry - 1), str(p.get("display_name") or "?"), font=name_font, fill=WHITE)
            bits = []
            if prof.get("position"):
                bits.append(prof["position"])
            if prof.get("champion"):
                bits.append(prof["champion"])
            if prof.get("win_rate") is not None:
                bits.append(f"승률 {prof['win_rate']}% ({prof['games']}판)")
            if not bits:
                bits.append("내전 전적 없음")
            draw.text((tx, ry + 31), " · ".join(bits)[:44], font=sub_font, fill=DIM)
            mmr_text = str(p.get("mmr") or "?")
            draw.text((rx + col_w - draw.textlength(mmr_text, font=mmr_font), ry + 2),
                      mmr_text, font=mmr_font, fill=(*color, 255))

    _draw_side(blue_players, True, BLUE)
    _draw_side(red_players, False, RED)

    # ── 밸런스 게이지 — 평균 MMR 의 Elo 기대 승률 ────────────────────
    try:
        b_avg, r_avg = float(blue.get("avg_mmr") or 0), float(red.get("avg_mmr") or 0)
        blue_expected = 1.0 / (1.0 + 10 ** ((r_avg - b_avg) / 400))
    except (TypeError, ValueError, ZeroDivisionError):
        blue_expected = 0.5
    gy = 574
    gx0, gx1 = x, W - MARGIN
    split = gx0 + int((gx1 - gx0) * blue_expected)
    draw.rounded_rectangle((gx0, gy, gx1, gy + 18), radius=9, fill=(255, 255, 255, 26))
    draw.rounded_rectangle((gx0, gy, split, gy + 18), radius=9, fill=(*BLUE, 190))
    draw.rounded_rectangle((split, gy, gx1, gy + 18), radius=9, fill=(*RED, 190))
    gauge_font = _font("SemiBold", 19)
    draw.text((gx0, gy - 28), f"블루 기대 승률 {blue_expected * 100:.0f}%", font=gauge_font,
              fill=(*BLUE, 255))
    red_label = f"{(1 - blue_expected) * 100:.0f}% 레드"
    draw.text((gx1 - draw.textlength(red_label, font=gauge_font), gy - 28), red_label,
              font=gauge_font, fill=(*RED, 255))

    _footer(draw)
    return _png(card)


# ═══════════════════════════════════════════════════════════════════════
# 5) 경기 MVP 카드 (자동 리포트 첨부용 배너)
# ═══════════════════════════════════════════════════════════════════════
MVP_H = 450  # 채널 피드에 매 경기 올라가는 배너라 표준 카드보다 낮게 잡는다.


def render_match_mvp_card(db: Session, match_id: str) -> bytes | None:
    m = db.query(Match).filter(Match.match_id == match_id).first()
    if not m or (m.game_duration or 0) < MIN_VALID_DURATION:
        return None
    rows = (
        db.query(MatchParticipant, Player.display_name)
        .outerjoin(Player, Player.id == MatchParticipant.player_id)
        .filter(MatchParticipant.match_id == match_id)
        .all()
    )
    if not rows:
        return None

    parts = []
    for p, display_name in rows:
        parts.append({
            "name": display_name or p.summoner_name,
            "champion": resolve_champion_name(p.champion_id, p.champion_name),
            "position": position_label(normalize_position(p.position)),
            "team_id": p.team_id,
            "win": bool(p.win),
            "kills": int(p.kills or 0),
            "deaths": int(p.deaths or 0),
            "assists": int(p.assists or 0),
            "ai_score": float(p.ai_score or 0),
            "dpm": float(p.total_damage_dealt or 0) / max(1.0, float(m.game_duration or 0) / 60),
            "raw_data": p.raw_data,
        })
    mvp = next((p for p in parts if records_service._parse_flags(p["raw_data"])["mvp"]), None)
    if not mvp:
        winners = [p for p in parts if p["win"]]
        if not winners:
            return None
        mvp = max(winners, key=lambda p: p["ai_score"])
        if mvp["ai_score"] <= 0:
            return None

    team_kills = sum(p["kills"] for p in parts if p["team_id"] == mvp["team_id"])
    kp = round((mvp["kills"] + mvp["assists"]) / team_kills * 100) if team_kills else None
    accent = BLUE if mvp["team_id"] == 100 else RED

    # ── 배경 — 오른쪽 스플래시 + 왼쪽 텍스트 패널 ────────────────────
    card = Image.new("RGB", (W, MVP_H), BG)
    splash = _champion_splash(mvp["champion"])
    if splash:
        card.paste(_cover_crop(splash, W, MVP_H), (0, 0))
    overlay = Image.new("RGBA", (W, MVP_H), (0, 0, 0, 0))
    overlay.alpha_composite(_horizontal_gradient(W, MVP_H, (10, 15, 28), 250, 24, 0.72))
    overlay.alpha_composite(_vertical_gradient(W, MVP_H, (8, 12, 24), 0, 150))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W, 5), fill=(*GOLD, 255))
    draw.rectangle((0, 0, W - 1, MVP_H - 1), outline=(255, 255, 255, 36), width=1)

    x = MARGIN
    header_font = _font("SemiBold", 20)
    crown = _emoji_image("👑", 22)
    hx = x
    if crown:
        card.paste(crown, (hx, 36), crown)
        draw = ImageDraw.Draw(card, "RGBA")
        hx += 32
    draw.text((hx, 34), "M A T C H  M V P", font=header_font, fill=(*GOLD, 255))
    played = datetime.fromtimestamp((m.game_creation or 0) / 1000, tz=KST)
    date_text = f"{played.strftime('%Y.%m.%d %H:%M')} · {int((m.game_duration or 0) / 60)}분"
    date_font = _font("Regular", 18)
    draw.text((W - MARGIN - draw.textlength(date_text, font=date_font), 36),
              date_text, font=date_font, fill=DIM)

    # 이름 + 배지
    name = mvp["name"]
    name_font = _font("Heavy", 60 if len(name) <= 8 else 48)
    draw.text((x - 2, 76), name, font=name_font, fill=WHITE)
    y = 158
    badge_font = _font("SemiBold", 21)
    bx = x
    bx = _pill(draw, (bx, y), f"{mvp['champion']} · {mvp['position']}", badge_font,
               fg=WHITE, outline=(255, 255, 255, 90))
    bx = _pill(draw, (bx, y), f"{'블루' if mvp['team_id'] == 100 else '레드'}팀 승리", badge_font,
               fg=(*accent, 255), outline=(*accent, 150))

    # 지표 4종
    y = 246
    stats = [
        ("KDA", f"{mvp['kills']}/{mvp['deaths']}/{mvp['assists']}"),
        ("AI점수", f"{mvp['ai_score']:.1f}"),
        ("DPM", f"{mvp['dpm']:.0f}"),
        ("킬관여", f"{kp}%" if kp is not None else "-"),
    ]
    value_font = _font("ExtraBold", 38)
    stat_label_font = _font("Regular", 18)
    col_w = 620 // 4
    for i, (label, value) in enumerate(stats):
        cx = x + i * col_w
        draw.text((cx, y), value, font=value_font, fill=WHITE)
        draw.text((cx, y + 48), label, font=stat_label_font, fill=DIM)

    # 한 줄 코멘트
    comment_font = _font("Medium", 22)
    deaths = mvp["deaths"]
    if deaths == 0:
        comment = "노데스 캐리, 흠잡을 곳이 없다"
    elif kp is not None and kp >= 70:
        comment = f"팀 킬의 {kp}%에 관여한 압도적 존재감"
    else:
        comment = "오늘 경기의 주인공"
    draw.text((x, 356), f"“{comment}”", font=comment_font, fill=(201, 211, 224, 255))

    footer_font = _font("Regular", 17)
    draw.text((W - MARGIN - draw.textlength(FOOTER, font=footer_font), MVP_H - 40),
              FOOTER, font=footer_font, fill=DIM)
    return _png(card)


# ═══════════════════════════════════════════════════════════════════════
# 6) 파워랭킹 카드
# ═══════════════════════════════════════════════════════════════════════
RANKING_MIN_GAMES = 3
RANKING_DELTA_DAYS = 7


def render_power_ranking_card(db: Session) -> bytes | None:
    # 순환 import 방지 — match_sync 는 서비스 계층(mmr_service)을 import 한다.
    from app.workers.match_sync import compute_elo_ratings

    now_ms = datetime.now(KST).timestamp() * 1000
    ratings, games_played = compute_elo_ratings(db)
    prev_ratings, _ = compute_elo_ratings(
        db, until_epoch_ms=int(now_ms - RANKING_DELTA_DAYS * 86400 * 1000))

    players = {p.id: p for p in db.query(Player).all()}
    ranked = sorted(
        (pid for pid, g in games_played.items() if g >= RANKING_MIN_GAMES),
        key=lambda pid: ratings[pid],
        reverse=True,
    )[:10]
    if not ranked:
        return None

    # 전적(승/패) — 등록 플레이어 행에서 집계.
    rows = (
        db.query(MatchParticipant)
        .join(Match, Match.match_id == MatchParticipant.match_id)
        .filter(
            MatchParticipant.player_id.in_(ranked),
            Match.game_duration >= MIN_VALID_DURATION,
        )
        .all()
    )
    record: dict[int, list[int]] = {pid: [0, 0] for pid in ranked}  # [games, wins]
    top_champ: dict[int, Counter] = {pid: Counter() for pid in ranked}
    for p in rows:
        pid = int(p.player_id)
        record[pid][0] += 1
        record[pid][1] += 1 if p.win else 0
        top_champ[pid][resolve_champion_name(p.champion_id, p.champion_name)] += 1

    # ── 배경 — 1위 주력 챔피언 스플래시를 은은하게 ───────────────────
    card = Image.new("RGB", (W, H), (13, 15, 24))
    leader_champ = top_champ[ranked[0]].most_common(1)
    splash = _champion_splash(leader_champ[0][0]) if leader_champ else None
    if splash:
        card.paste(_cover_crop(splash, W, H), (0, 0))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(_horizontal_gradient(W, H, (13, 15, 24), 252, 178, 0.6))
    overlay.alpha_composite(_vertical_gradient(W, H, (10, 11, 18), 80, 215))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card, "RGBA")
    draw.rectangle((0, 0, W, 5), fill=(*GOLD, 255))
    draw.rectangle((0, 0, W - 1, H - 1), outline=(*GOLD, 70), width=1)

    x = MARGIN
    draw.text((x, 40), "P O W E R  R A N K I N G", font=_font("SemiBold", 20), fill=(*GOLD, 255))
    title_font = _font("Heavy", 52)
    tx = x
    emoji = _emoji_image("⚡", 48)
    if emoji:
        card.paste(emoji, (tx, 74), emoji)
        draw = ImageDraw.Draw(card, "RGBA")
        tx += emoji.width + 16
    draw.text((tx, 68), "GOL 파워랭킹", font=title_font, fill=WHITE)
    sub_font = _font("Regular", 18)
    sub = (f"내전 Elo MMR 기준 · {RANKING_MIN_GAMES}판 이상 · "
           f"{datetime.now(KST).strftime('%Y.%m.%d')}")
    draw.text((W - MARGIN - draw.textlength(sub, font=sub_font), 44), sub, font=sub_font, fill=DIM)

    # ── 랭킹 행 — 1~5위 왼쪽, 6~10위 오른쪽 ─────────────────────────
    row_y0, row_h = 186, 78
    col_x = [x, W // 2 + 36]
    col_w = W // 2 - MARGIN - 48
    rank_font = _font("Heavy", 30)
    name_font = _font("Bold", 26)
    mmr_font = _font("ExtraBold", 27)
    sub_row_font = _font("Regular", 17)
    rank_colors = [(*GOLD, 255), (198, 208, 224, 255), (205, 148, 96, 255)]

    for i, pid in enumerate(ranked):
        cx = col_x[i // 5]
        ry = row_y0 + (i % 5) * row_h
        color = rank_colors[i] if i < 3 else (120, 132, 152, 255)
        rank_text = str(i + 1)
        draw.text((cx + (30 - draw.textlength(rank_text, font=rank_font)) / 2, ry),
                  rank_text, font=rank_font, fill=color)
        name = players[pid].display_name or f"Player {pid}"
        draw.text((cx + 46, ry - 2), name[:10], font=name_font, fill=WHITE)

        mmr_now = int(round(ratings[pid]))
        mmr_text = str(mmr_now)
        draw.text((cx + col_w - draw.textlength(mmr_text, font=mmr_font), ry - 2),
                  mmr_text, font=mmr_font, fill=(*GOLD, 255) if i < 3 else GRAY)

        games, wins = record[pid]
        delta = int(round(ratings[pid] - prev_ratings.get(pid, ratings[pid])))
        if delta >= 1:
            delta_text, delta_color = f"▲{delta}", (74, 222, 128, 255)
        elif delta <= -1:
            delta_text, delta_color = f"▼{-delta}", (248, 113, 113, 255)
        else:
            delta_text, delta_color = "—", DIM
        wr = round(wins / games * 100) if games else 0
        detail = f"{wins}승 {games - wins}패 · 승률 {wr}%"
        draw.text((cx + 46, ry + 32), detail, font=sub_row_font, fill=DIM)
        draw.text((cx + 46 + draw.textlength(detail, font=sub_row_font) + 12, ry + 32),
                  f"{delta_text} (7일)", font=sub_row_font, fill=delta_color)
        if i % 5 != 4:
            draw.line((cx, ry + row_h - 12, cx + col_w, ry + row_h - 12),
                      fill=(255, 255, 255, 22), width=1)

    _footer(draw)
    return _png(card)
