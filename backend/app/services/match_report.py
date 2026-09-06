"""경기 단위 리포트 생성 — 팀 스탯 + 어워드(MVP/에이스/클러치 등) + 경기 흐름.

경기 종료 직후 디스코드 채널에 자동 포스팅되는 리포트와 /최근경기 명령이
같은 데이터를 쓴다. 입력은 ORM이 아닌 dict 로 받아 서비스가 DB 계층과
결합하지 않게 한다.
"""

from __future__ import annotations

import json
from typing import Any

POSITION_ORDER = {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4, "UNKNOWN": 9}
POSITION_KR = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿", "UNKNOWN": "?"}
# concat_events_dict 의 killer/target 코드("R_Jun")에 쓰이는 포지션 약어.
ROLE_CODE_TO_POSITION = {"Top": "TOP", "Jun": "JUNGLE", "Mid": "MIDDLE", "Bot": "BOTTOM", "Sup": "UTILITY"}
EVENT_TYPE_KR = {"Champion": "킬", "Building": "건물 파괴", "Monster": "오브젝트"}

# 재시작(리메이크)으로 판정할 최소 경기 시간(초).
MIN_VALID_DURATION = 300


def _load(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds or 0)), 60)
    return f"{minutes}분 {secs:02d}초"


def _participant_view(p: dict) -> dict[str, Any]:
    """participant dict(컬럼 + raw_data)를 리포트 표시용으로 정리한다."""
    raw = _load(p.get("raw_data")) or {}
    duration_min = max(1.0, float(p.get("game_duration") or 0) / 60.0)
    # 주의: DeepLOL 필드명은 직관과 반대다. total_damage_dealt 가 '챔피언 상대 딜량'이고
    # total_damage_dealt_to_champion(s) 이 미니언/몹 포함 총딜이다. DPM/딜왕은 챔피언 딜 기준.
    damage = int(p.get("total_damage_dealt") or 0)
    if damage <= 0:
        try:
            damage = int(raw.get("total_damage_dealt") or 0)
        except (TypeError, ValueError):
            damage = 0
    position = p.get("position") or "UNKNOWN"
    return {
        "name": p.get("display_name") or p.get("summoner_name") or "?",
        "registered": p.get("player_id") is not None,
        "champion": p.get("champion_name") or "?",
        "position": position,
        "position_label": POSITION_KR.get(position, "?"),
        "team_id": int(p.get("team_id") or 0),
        "win": bool(p.get("win")),
        "kda_text": f"{int(p.get('kills') or 0)}/{int(p.get('deaths') or 0)}/{int(p.get('assists') or 0)}",
        "ai_score": round(float(p.get("ai_score") or 0), 1),
        "dpm": round(damage / duration_min),
        "damage": damage,
        "cs": int(p.get("cs") or 0),
        "wards_placed": int(p.get("wards_placed") or 0),
        "mvp": bool(raw.get("mvp")),
        "ace": bool(raw.get("ace")),
    }


def _pick_award(views: list[dict], key: str, *, team_win: bool | None = None) -> dict | None:
    pool = [v for v in views if team_win is None or v["win"] == team_win]
    if not pool:
        return None
    best = max(pool, key=lambda v: v[key])
    if best[key] <= 0:
        return None
    return best


def _clutch_award(match_raw: dict, views: list[dict]) -> dict | None:
    """concat_events_dict 에서 승률 변동(|win_rate_delta|)이 가장 큰 이벤트를 찾는다."""
    ta = match_raw.get("time_analysis") or {}
    events_groups = ta.get("concat_events_dict") or {}
    best: dict | None = None
    groups = events_groups.values() if isinstance(events_groups, dict) else events_groups
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.values():
            if not isinstance(entry, dict):
                continue
            try:
                delta = float(entry.get("win_rate_delta") or 0)
            except (TypeError, ValueError):
                continue
            if best is None or abs(delta) > abs(best["delta"]):
                event = entry.get("event") or {}
                best = {"delta": delta, "event": event, "minute": event.get("minute_float") or event.get("minute") or 0}
    if not best or abs(best["delta"]) < 3.0:  # 미미한 변동은 클러치로 치지 않는다.
        return None

    event = best["event"]
    killer_code = str(event.get("killer") or "")  # 예: "R_Jun"
    side = "RED" if killer_code.startswith("R_") else "BLUE" if killer_code.startswith("B_") else None
    position = ROLE_CODE_TO_POSITION.get(killer_code.split("_")[-1]) if "_" in killer_code else None
    actor = None
    if side and position:
        team_id = 100 if side == "BLUE" else 200
        actor = next((v for v in views if v["team_id"] == team_id and v["position"] == position), None)

    type_kr = EVENT_TYPE_KR.get(str(event.get("type")), "교전")
    swing = abs(best["delta"])
    minute = best["minute"]
    return {
        "emoji": "⚡",
        "title": "클러치",
        "name": actor["name"] if actor else (killer_code or "?"),
        "champion": actor["champion"] if actor else "",
        "detail": f"{minute:.0f}분 {type_kr}로 팀 승률 {swing:.1f}%p 스윙",
    }


def _flow_summary(match_raw: dict, blue_win: bool) -> dict | None:
    """분당 승률(concat_seq_dict)로 경기 흐름을 요약한다.

    time_analysis 승률값은 50 근처로 압축돼 있어 상대 임계(43/57)를 쓴다
    (player_analysis._momentum_analysis 와 동일 원칙).
    """
    ta = match_raw.get("time_analysis") or {}
    seq = ta.get("concat_seq_dict") or {}
    blue_series: list[float] = []
    for mk in sorted(seq, key=lambda x: int(x) if str(x).isdigit() else 0):
        v = seq[mk]
        if isinstance(v, dict) and "B_Avg" in v:
            try:
                blue_series.append(float(v["B_Avg"]))
            except (TypeError, ValueError):
                pass
    if len(blue_series) < 3:
        return None

    winner_series = blue_series if blue_win else [100 - x for x in blue_series]
    lo, hi = min(winner_series), max(winner_series)
    winner_label = "블루팀" if blue_win else "레드팀"
    if lo <= 43:
        text = f"{winner_label}이 한때 승률 {lo:.0f}%까지 밀렸지만 뒤집은 역전승"
        kind = "comeback"
    elif lo >= 47:
        text = f"{winner_label}이 시종일관 리드를 지킨 완승 (최저 {lo:.0f}%)"
        kind = "wire_to_wire"
    else:
        text = f"{winner_label}이 접전 끝에 승리 (승률 {lo:.0f}~{hi:.0f}% 등락)"
        kind = "close"
    return {
        "kind": kind,
        "winner_min_wr": round(lo, 1),
        "winner_max_wr": round(hi, 1),
        "text": text,
    }


def build_match_report(match: dict, participants: list[dict]) -> dict[str, Any] | None:
    """한 경기의 리포트 dict 를 만든다. 유효하지 않은 경기(리메이크 등)는 None."""
    duration = int(match.get("game_duration") or 0)
    if duration < MIN_VALID_DURATION or len(participants) < 10:
        return None

    match_raw = _load(match.get("raw_data")) or {}
    basic = match_raw.get("match_basic_dict") or {}

    for p in participants:
        p.setdefault("game_duration", duration)
    views = [_participant_view(p) for p in participants]
    views.sort(key=lambda v: POSITION_ORDER.get(v["position"], 9))
    blue = [v for v in views if v["team_id"] == 100]
    red = [v for v in views if v["team_id"] == 200]
    if not blue or not red:
        return None

    blue_win = blue[0]["win"]
    game_creation_ms = int(match.get("game_creation") or 0)
    game_end_epoch = game_creation_ms / 1000 + duration

    # ── 어워드 ──────────────────────────────────────────────────────────
    awards: list[dict] = []
    mvp = next((v for v in views if v["mvp"]), None) or _pick_award(views, "ai_score", team_win=True)
    if mvp:
        awards.append({
            "emoji": "🏆", "title": "MVP", "name": mvp["name"], "champion": mvp["champion"],
            "detail": f"AI점수 {mvp['ai_score']} · {mvp['kda_text']} · DPM {mvp['dpm']}",
        })
    ace = next((v for v in views if v["ace"]), None) or _pick_award(views, "ai_score", team_win=False)
    if ace:
        awards.append({
            "emoji": "🛡️", "title": "에이스 (패배팀 최고)", "name": ace["name"], "champion": ace["champion"],
            "detail": f"AI점수 {ace['ai_score']} · {ace['kda_text']} · DPM {ace['dpm']}",
        })
    clutch = _clutch_award(match_raw, views)
    if clutch:
        awards.append(clutch)
    for key, emoji, title, fmt in (
        ("damage", "🩸", "딜왕", lambda v: f"챔피언 딜량 {v['damage']:,} (DPM {v['dpm']})"),
        ("wards_placed", "👁️", "시야왕", lambda v: f"와드 {v['wards_placed']}개 설치"),
        ("cs", "🌾", "CS왕", lambda v: f"CS {v['cs']} ({v['cs'] / max(1, duration / 60):.1f}/분)"),
    ):
        top = _pick_award(views, key)
        if top:
            awards.append({
                "emoji": emoji, "title": title, "name": top["name"], "champion": top["champion"],
                "detail": fmt(top),
            })

    return {
        "match_id": match.get("match_id"),
        "match_name": basic.get("tournament_name") or None,
        "game_creation": game_creation_ms,
        "game_end_epoch": int(game_end_epoch),
        "duration_seconds": duration,
        "duration_text": _fmt_duration(duration),
        "winner_team": "BLUE" if blue_win else "RED",
        "teams": {"blue": blue, "red": red},
        "awards": awards,
        "flow": _flow_summary(match_raw, blue_win),
    }
