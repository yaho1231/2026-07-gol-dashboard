"""주간 리포트 — 지난 7일 내전 결산 다이제스트.

봇이 매주 월요일 공지 채널에 자동 발행하고, /주간리포트 명령도 같은 데이터를 쓴다.
섹션은 {emoji, title, lines[]} 구조라 봇은 그대로 임베드 필드로 렌더링하면 된다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services import records_service

KST = timezone(timedelta(hours=9))

# 주간 순위(승률왕·폼)에 들어가기 위한 최소 주간 경기 수.
MIN_WEEKLY_GAMES = 3
# 폼 변화 비교에 필요한 이전(주간 이전) 최소 경기 수.
MIN_PRIOR_GAMES = 5


def _parse_pings(raw: Any) -> float:
    if not raw:
        return 0.0
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        pings = payload.get("total_pings_dict") or {}
        return float(sum(v for v in pings.values() if isinstance(v, (int, float))))
    except (json.JSONDecodeError, TypeError):
        return 0.0


def build_weekly_report(db: Session, days: int = 7) -> dict[str, Any]:
    now = time.time()
    since_ms = (now - days * 86400) * 1000

    rows = records_service._load_rows(db)
    week_rows = [r for r in rows if r["game_creation"] >= since_ms]
    prior_rows = [r for r in rows if r["game_creation"] < since_ms]

    start_label = datetime.fromtimestamp(now - days * 86400, tz=KST).strftime("%-m/%-d")
    end_label = datetime.fromtimestamp(now, tz=KST).strftime("%-m/%-d")
    period_label = f"{start_label} ~ {end_label}"

    if not week_rows:
        return {"games": 0, "period_label": period_label, "sections": []}

    by_match: dict[str, list[dict[str, Any]]] = {}
    for r in week_rows:
        by_match.setdefault(r["match_id"], []).append(r)

    total_games = len(by_match)
    total_seconds = sum(ms[0]["game_duration"] for ms in by_match.values())
    registered = [r for r in week_rows if r["player_id"]]
    unique_players = len({r["player_id"] for r in registered})

    # ── 플레이어별 주간 집계 (등록 플레이어만) ──────────────────────────
    stats: dict[str, dict[str, Any]] = {}
    for r in registered:
        s = stats.setdefault(r["name"], {
            "games": 0, "wins": 0, "deaths": 0, "wards": 0.0, "pings": 0.0,
            "ai_sum": 0.0, "mvp": 0, "best_dpm": 0.0, "best_dpm_champ": "",
        })
        s["games"] += 1
        s["wins"] += 1 if r["win"] else 0
        s["deaths"] += r["deaths"]
        s["wards"] += r["wards_placed"]
        s["pings"] += _parse_pings(r["raw_data"])
        s["ai_sum"] += r["ai_score"]
        if r["dpm"] > s["best_dpm"]:
            s["best_dpm"] = r["dpm"]
            s["best_dpm_champ"] = r["champion"]

    for match_rows in by_match.values():
        mvp = records_service._match_mvp(match_rows)
        if mvp and mvp["player_id"] and mvp["name"] in stats:
            stats[mvp["name"]]["mvp"] += 1

    sections: list[dict[str, Any]] = []

    # ── 🏆 주간 MVP ────────────────────────────────────────────────────
    mvp_name, mvp_champion = None, None
    mvp_ranked = sorted(
        ((name, s) for name, s in stats.items() if s["mvp"] > 0),
        key=lambda kv: (kv[1]["mvp"], kv[1]["ai_sum"] / max(1, kv[1]["games"])),
        reverse=True,
    )
    if mvp_ranked:
        name, s = mvp_ranked[0]
        mvp_name = name
        avg_ai = s["ai_sum"] / max(1, s["games"])
        # MVP를 가장 많이 딴 경기의 챔피언(썸네일용) — 주간 첫 MVP 경기 기준.
        for match_rows in sorted(by_match.values(), key=lambda ms: ms[0]["game_creation"]):
            mvp = records_service._match_mvp(match_rows)
            if mvp and mvp["name"] == name:
                mvp_champion = mvp["champion"]
                break
        lines = [f"👑 **{name}** — MVP {s['mvp']}회 · {s['games']}판 {s['wins']}승 · 평균 AI {avg_ai:.1f}"]
        runner_ups = [f"{n} {st['mvp']}회" for n, st in mvp_ranked[1:3]]
        if runner_ups:
            lines.append(f"┗ 뒤이어 {' · '.join(runner_ups)}")
        sections.append({"emoji": "🏆", "title": "주간 MVP", "lines": lines})

    # ── 🔥 이주의 순위 ─────────────────────────────────────────────────
    rank_lines = []
    win_ranked = sorted(stats.items(), key=lambda kv: (kv[1]["wins"], kv[1]["wins"] / max(1, kv[1]["games"])), reverse=True)
    if win_ranked and win_ranked[0][1]["wins"] > 0:
        name, s = win_ranked[0]
        rank_lines.append(f"🥇 **다승왕** — {name} ({s['wins']}승 {s['games'] - s['wins']}패)")
    games_ranked = sorted(stats.items(), key=lambda kv: kv[1]["games"], reverse=True)
    if games_ranked:
        name, s = games_ranked[0]
        rank_lines.append(f"🎮 **최다 출전** — {name} ({s['games']}판)")
    wr_pool = [(n, s) for n, s in stats.items() if s["games"] >= MIN_WEEKLY_GAMES]
    if wr_pool:
        name, s = max(wr_pool, key=lambda kv: (kv[1]["wins"] / kv[1]["games"], kv[1]["games"]))
        wr = s["wins"] / s["games"] * 100
        rank_lines.append(f"📊 **승률왕** — {name} ({wr:.0f}% · {s['games']}판, {MIN_WEEKLY_GAMES}판 이상)")
    # 최다 연승 — 주간 창 안의 경기만 시간순으로 세므로 주 경계를 넘는 연승은 끊긴다.
    by_player_week: dict[str, list[dict[str, Any]]] = {}
    for r in registered:
        by_player_week.setdefault(r["name"], []).append(r)
    streak_name, streak_best = None, 0
    for name, player_rows in by_player_week.items():
        player_rows.sort(key=lambda r: r["game_creation"])
        cur = best = 0
        for r in player_rows:
            cur = cur + 1 if r["win"] else 0
            best = max(best, cur)
        if best > streak_best:
            streak_name, streak_best = name, best
    if streak_name and streak_best >= 3:
        rank_lines.append(f"⚡ **최다 연승** — {streak_name} ({streak_best}연승)")
    if rank_lines:
        sections.append({"emoji": "🔥", "title": "이주의 순위", "lines": rank_lines})

    # ── 📈 폼 상승 / 📉 폼 하락 ────────────────────────────────────────
    prior_stats: dict[str, dict[str, float]] = {}
    for r in prior_rows:
        if not r["player_id"]:
            continue
        p = prior_stats.setdefault(r["name"], {"games": 0, "wins": 0})
        p["games"] += 1
        p["wins"] += 1 if r["win"] else 0

    form_deltas = []
    for name, s in stats.items():
        prior = prior_stats.get(name)
        if s["games"] < MIN_WEEKLY_GAMES or not prior or prior["games"] < MIN_PRIOR_GAMES:
            continue
        weekly_wr = s["wins"] / s["games"] * 100
        prior_wr = prior["wins"] / prior["games"] * 100
        form_deltas.append((name, weekly_wr - prior_wr, weekly_wr, s))
    if form_deltas:
        form_lines = []
        up = max(form_deltas, key=lambda t: t[1])
        if up[1] >= 10:
            name, delta, wr, s = up
            form_lines.append(f"📈 **폼 상승** — {name} (주간 승률 {wr:.0f}%, 통산 대비 +{delta:.0f}%p)")
        down = min(form_deltas, key=lambda t: t[1])
        if down[1] <= -10:
            name, delta, wr, s = down
            form_lines.append(f"📉 **폼 주의** — {name} (주간 승률 {wr:.0f}%, 통산 대비 {delta:.0f}%p)")
        if form_lines:
            sections.append({"emoji": "🌡️", "title": "폼 체크", "lines": form_lines})

    # ── 🎭 이주의 어워드 (유쾌 어워드) ─────────────────────────────────
    award_lines = []
    dpm_ranked = sorted(stats.items(), key=lambda kv: kv[1]["best_dpm"], reverse=True)
    if dpm_ranked and dpm_ranked[0][1]["best_dpm"] > 0:
        name, s = dpm_ranked[0]
        award_lines.append(f"💥 **주간 한 방** — {name} ({s['best_dpm_champ']} DPM {s['best_dpm']:.0f})")
    ward_pool = [(n, s) for n, s in stats.items() if s["games"] >= 2]
    if ward_pool:
        name, s = max(ward_pool, key=lambda kv: kv[1]["wards"] / kv[1]["games"])
        if s["wards"] > 0:
            award_lines.append(f"🕯️ **시야 요정** — {name} (경기당 와드 {s['wards'] / s['games']:.1f}개)")
    ping_pool = [(n, s) for n, s in stats.items() if s["games"] >= 2 and s["pings"] > 0]
    if ping_pool:
        name, s = max(ping_pool, key=lambda kv: kv[1]["pings"] / kv[1]["games"])
        award_lines.append(f"📢 **핑 폭격기** — {name} (경기당 핑 {s['pings'] / s['games']:.0f}회)")
    death_ranked = sorted(stats.items(), key=lambda kv: kv[1]["deaths"], reverse=True)
    if death_ranked and death_ranked[0][1]["deaths"] > 0:
        name, s = death_ranked[0]
        award_lines.append(f"💀 **이주의 불사조** — {name} (누적 {s['deaths']}데스, 그래도 계속 싸웠다)")
    if award_lines:
        sections.append({"emoji": "🎭", "title": "이주의 어워드", "lines": award_lines})

    # ── 🚨 이번 주 신기록 ──────────────────────────────────────────────
    broken = records_service.records_broken_since(db, since_ms)
    if broken:
        record_lines = [
            f"{b['emoji']} **{b['label']}** — {b['name']} ({b['champion']}) {b['value_text']}"
            for b in broken[:6]
        ]
        sections.append({"emoji": "🚨", "title": "이번 주 신기록", "lines": record_lines})

    return {
        "period_label": period_label,
        "games": total_games,
        "hours": round(total_seconds / 3600, 1),
        "players": unique_players,
        "mvp_name": mvp_name,
        "mvp_champion": mvp_champion,
        "sections": sections,
    }
