"""서버 기록(단일 경기 신기록) 감지 + 명예의 전당 집계.

- 경기 자동 리포트에 "🚨 서버 신기록" 필드를 붙이기 위한 신기록 감지
- /명예의전당 명령이 쓰는 통산 기록 보드(기록 보유자·누적 MVP·다승 등)
- 주간 리포트가 쓰는 '이번 주에 깨진 기록' 목록

모든 판정은 리메이크(300초 미만) 경기를 제외하고, 등록 여부와 무관하게
경기에 남은 모든 참가자 행을 대상으로 한다(이름은 등록명 우선).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.match import Match, MatchParticipant
from app.models.player import Player
from app.services.champion_data import resolve_champion_name
from app.services.match_report import MIN_VALID_DURATION

# 신기록 공지는 표본이 어느 정도 쌓인 뒤부터 의미가 있다(첫 경기 = 전부 신기록 방지).
MIN_HISTORY_MATCHES = 10

# 단일 경기 기록 정의. value(row) 로 값을 뽑고, 큰 값이 기록이다.
RECORD_DEFS: list[dict[str, Any]] = [
    {"key": "kills", "emoji": "⚔️", "label": "한 경기 최다 킬",
     "value": lambda r: float(r["kills"]), "fmt": lambda v: f"{v:.0f}킬"},
    {"key": "assists", "emoji": "🤝", "label": "한 경기 최다 어시스트",
     "value": lambda r: float(r["assists"]), "fmt": lambda v: f"{v:.0f}어시"},
    {"key": "dpm", "emoji": "🩸", "label": "역대 최고 DPM",
     "value": lambda r: r["dpm"], "fmt": lambda v: f"{v:.0f}"},
    {"key": "ai_score", "emoji": "🤖", "label": "역대 최고 AI점수",
     "value": lambda r: float(r["ai_score"]), "fmt": lambda v: f"{v:.1f}점"},
    {"key": "cs", "emoji": "🌾", "label": "한 경기 최다 CS",
     "value": lambda r: float(r["cs"]), "fmt": lambda v: f"{v:.0f}"},
    {"key": "wards_placed", "emoji": "👁️", "label": "한 경기 최다 와드",
     "value": lambda r: float(r["wards_placed"]), "fmt": lambda v: f"{v:.0f}개"},
]


def _load_rows(db: Session) -> list[dict[str, Any]]:
    """유효 경기(리메이크 제외)의 참가자 행을 기록 판정용 dict 목록으로 만든다."""
    query = (
        db.query(MatchParticipant, Match.game_creation, Match.game_duration, Player.display_name)
        .join(Match, Match.match_id == MatchParticipant.match_id)
        .outerjoin(Player, Player.id == MatchParticipant.player_id)
        .filter(Match.game_duration >= MIN_VALID_DURATION)
    )
    rows: list[dict[str, Any]] = []
    for p, game_creation, game_duration, display_name in query.all():
        minutes = max(1.0, float(game_duration or 0) / 60.0)
        rows.append({
            "match_id": p.match_id,
            "game_creation": int(game_creation or 0),
            "game_duration": int(game_duration or 0),
            "player_id": p.player_id,
            "name": display_name or p.summoner_name or "?",
            "champion": resolve_champion_name(p.champion_id, p.champion_name),
            "win": bool(p.win),
            "kills": int(p.kills or 0),
            "deaths": int(p.deaths or 0),
            "assists": int(p.assists or 0),
            "cs": int(p.cs or 0),
            "wards_placed": int(p.wards_placed or 0),
            "ai_score": float(p.ai_score or 0),
            "dpm": float(p.total_damage_dealt or 0) / minutes,
            "quadra_kills": int(p.quadra_kills or 0),
            "penta_kills": int(p.penta_kills or 0),
            "raw_data": p.raw_data,
        })
    return rows


def _ro_particle(word: str) -> str:
    """받침에 따라 '로'/'으로'를 고른다(ㄹ 받침은 '로')."""
    if not word:
        return "로"
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        final = (code - 0xAC00) % 28
        return "로" if final in (0, 8) else "으로"  # 8 = ㄹ 받침
    return "로"


def _parse_flags(raw: Any) -> dict[str, bool]:
    """raw_data 에서 mvp/ace 플래그만 뽑는다."""
    if not raw:
        return {"mvp": False, "ace": False}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        return {"mvp": bool(payload.get("mvp")), "ace": bool(payload.get("ace"))}
    except (json.JSONDecodeError, TypeError):
        return {"mvp": False, "ace": False}


def _match_mvp(match_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """경기 MVP — DeepLOL mvp 플래그 우선, 없으면 승리팀 최고 AI점수.

    match_report.build_match_report 의 MVP 선정 로직과 같은 원칙."""
    flagged = next((r for r in match_rows if _parse_flags(r["raw_data"])["mvp"]), None)
    if flagged:
        return flagged
    winners = [r for r in match_rows if r["win"]]
    if not winners:
        return None
    best = max(winners, key=lambda r: r["ai_score"])
    return best if best["ai_score"] > 0 else None


def _best_row(rows: list[dict[str, Any]], value_fn) -> dict[str, Any] | None:
    best = None
    best_val = 0.0
    for r in rows:
        v = value_fn(r)
        if v > best_val:
            best, best_val = r, v
    return best


def new_records_for_match(db: Session, match_id: str) -> list[dict[str, Any]]:
    """이 경기에서 갱신된 서버 기록 + 멀티킬(쿼드라/펜타) 이벤트 목록.

    경기 자동 리포트/최근경기 임베드의 "🚨 서버 신기록" 필드에 쓰인다."""
    rows = _load_rows(db)
    current = [r for r in rows if r["match_id"] == match_id]
    if not current:
        return []
    creation = current[0]["game_creation"]
    earlier = [r for r in rows if r["game_creation"] < creation]

    entries: list[dict[str, Any]] = []

    # 멀티킬 이벤트 — 기록 여부와 무관하게 달성 자체가 이벤트다.
    for r in current:
        if r["penta_kills"] > 0:
            entries.append({
                "emoji": "🌟", "label": "펜타킬!", "name": r["name"], "champion": r["champion"],
                "detail": f"{r['champion']}{_ro_particle(r['champion'])} 펜타킬 달성",
            })
        elif r["quadra_kills"] > 0:
            entries.append({
                "emoji": "💥", "label": "쿼드라킬", "name": r["name"], "champion": r["champion"],
                "detail": f"{r['champion']}{_ro_particle(r['champion'])} 쿼드라킬 달성",
            })

    # 신기록 판정은 이전 경기 표본이 충분할 때만.
    if len({r["match_id"] for r in earlier}) < MIN_HISTORY_MATCHES:
        return entries

    for rec in RECORD_DEFS:
        cur_best = _best_row(current, rec["value"])
        if not cur_best:
            continue
        prev_best = _best_row(earlier, rec["value"])
        prev_val = rec["value"](prev_best) if prev_best else 0.0
        cur_val = rec["value"](cur_best)
        if cur_val > prev_val and prev_best:
            entries.append({
                "emoji": rec["emoji"],
                "label": rec["label"],
                "name": cur_best["name"],
                "champion": cur_best["champion"],
                "detail": (
                    f"{rec['fmt'](cur_val)} — 종전 {rec['fmt'](prev_val)}"
                    f" ({prev_best['name']}) 경신"
                ),
            })
    return entries


def records_broken_since(db: Session, since_epoch_ms: float) -> list[dict[str, Any]]:
    """since 이후 경기들에서 깨진 서버 기록 목록(주간 리포트용).

    경기를 시간순으로 재생하며 최고 기록을 갱신 추적한다."""
    rows = _load_rows(db)
    by_match: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(r)
    ordered = sorted(by_match.values(), key=lambda ms: ms[0]["game_creation"])

    broken: list[dict[str, Any]] = []
    best_vals: dict[str, float] = {rec["key"]: 0.0 for rec in RECORD_DEFS}
    seen_matches = 0
    for match_rows in ordered:
        in_window = match_rows[0]["game_creation"] >= since_epoch_ms
        for rec in RECORD_DEFS:
            cur_best = _best_row(match_rows, rec["value"])
            if not cur_best:
                continue
            cur_val = rec["value"](cur_best)
            if cur_val > best_vals[rec["key"]]:
                if in_window and seen_matches >= MIN_HISTORY_MATCHES:
                    broken.append({
                        "emoji": rec["emoji"], "label": rec["label"],
                        "name": cur_best["name"], "champion": cur_best["champion"],
                        "value_text": rec["fmt"](cur_val),
                    })
                best_vals[rec["key"]] = cur_val
        if in_window:
            for r in match_rows:
                if r["penta_kills"] > 0:
                    broken.append({
                        "emoji": "🌟", "label": "펜타킬", "name": r["name"],
                        "champion": r["champion"], "value_text": "달성",
                    })
        seen_matches += 1
    return broken


def _format_date(epoch_ms: int) -> str:
    from datetime import datetime, timedelta, timezone

    kst = timezone(timedelta(hours=9))
    return datetime.fromtimestamp(epoch_ms / 1000, tz=kst).strftime("%y.%m.%d")


def build_hall_of_fame(db: Session) -> dict[str, Any]:
    """명예의 전당 — 단일 경기 기록 보유자 + 누적 보드(MVP/에이스/다승/출전)."""
    rows = _load_rows(db)
    if not rows:
        return {"records": [], "boards": []}

    # ── 단일 경기 기록 보유자 ──────────────────────────────────────────
    records = []
    for rec in RECORD_DEFS:
        best = _best_row(rows, rec["value"])
        if not best:
            continue
        records.append({
            "emoji": rec["emoji"],
            "label": rec["label"],
            "name": best["name"],
            "champion": best["champion"],
            "value_text": rec["fmt"](rec["value"](best)),
            "date_text": _format_date(best["game_creation"]),
        })

    # ── 누적 보드 (등록 플레이어만) ────────────────────────────────────
    by_match: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(r)

    mvp_counts: dict[str, int] = {}
    ace_counts: dict[str, int] = {}
    for match_rows in by_match.values():
        mvp = _match_mvp(match_rows)
        if mvp and mvp["player_id"]:
            mvp_counts[mvp["name"]] = mvp_counts.get(mvp["name"], 0) + 1
        ace = next((r for r in match_rows if _parse_flags(r["raw_data"])["ace"]), None)
        if ace and ace["player_id"]:
            ace_counts[ace["name"]] = ace_counts.get(ace["name"], 0) + 1

    wins: dict[str, int] = {}
    games: dict[str, int] = {}
    by_player: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not r["player_id"]:
            continue
        games[r["name"]] = games.get(r["name"], 0) + 1
        if r["win"]:
            wins[r["name"]] = wins.get(r["name"], 0) + 1
        by_player.setdefault(r["name"], []).append(r)

    streaks: dict[str, int] = {}
    for name, player_rows in by_player.items():
        player_rows.sort(key=lambda r: r["game_creation"])
        cur = best = 0
        for r in player_rows:
            cur = cur + 1 if r["win"] else 0
            best = max(best, cur)
        if best >= 2:
            streaks[name] = best

    def _top_lines(counter: dict[str, int], unit: str, top: int = 5) -> list[str]:
        medals = ["🥇", "🥈", "🥉", "4.", "5."]
        ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return [f"{medals[i]} **{name}** — {count}{unit}" for i, (name, count) in enumerate(ranked)]

    boards = []
    if mvp_counts:
        boards.append({"emoji": "🏆", "title": "누적 MVP", "lines": _top_lines(mvp_counts, "회")})
    if ace_counts:
        boards.append({"emoji": "🛡️", "title": "누적 에이스 (패배팀 최고)", "lines": _top_lines(ace_counts, "회", top=3)})
    if wins:
        boards.append({"emoji": "🔥", "title": "통산 다승", "lines": _top_lines(wins, "승")})
    if streaks:
        boards.append({"emoji": "⚡", "title": "최다 연승", "lines": _top_lines(streaks, "연승", top=3)})
    if games:
        boards.append({"emoji": "🎮", "title": "최다 출전", "lines": _top_lines(games, "판")})

    return {
        "records": records,
        "boards": boards,
        "total_matches": len(by_match),
    }
