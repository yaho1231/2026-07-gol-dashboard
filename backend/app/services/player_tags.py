"""지표 기반 플레이어 자동 태그.

경기 전적 DataFrame(match_participants)에서 매번 파생 계산하므로 별도 저장 없이
새 경기가 동기화되면 즉시 태그가 갱신된다. 지표 비교 기준은 같은 포지션의
리그 평균(플레이어의 포지션 구성비 가중)이라 서폿의 와드 수가 원딜 기준으로
평가되는 왜곡이 없다.

대시보드(상세분석·팀 시뮬레이터)에서 사용한다. player_analysis.py와 같은
pandas 기반 패턴 — build_baselines()로 리그 기준을 한 번 만들고,
compute_tags(player_id, baselines)를 플레이어마다 호출한다.

태그 형식: {"label", "emoji", "kind"(good|bad|neutral), "reason"} 3개.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

CANONICAL_POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

# 리그 기준과 비교할 파생 지표 컬럼.
_METRICS = [
    "kills", "deaths", "assists", "kda", "dpm", "cspm", "gpm",
    "wards", "dmg_taken", "ai", "kp", "mk",
]

# (지표, 지표 한글명, 높을 때 (임계배율, 라벨, 이모지, 종류), 낮을 때 (임계배율, ...) 또는 None, 근거 포맷)
# 임계배율은 포지션 가중 리그 평균 대비 비율. 낮을 때 스펙이 None이면 낮음은 태그하지 않는다.
_RATIO_SPECS: list[tuple[str, str, tuple, tuple | None, str]] = [
    ("kills", "킬", (1.30, "킬 캐처", "⚔️", "good"), None, "경기당 킬 {v:.1f} · 기준 {b:.1f}"),
    ("deaths", "데스", (1.25, "많은 죽음", "💀", "bad"), (0.75, "잘 안 죽음", "🛡️", "good"), "경기당 데스 {v:.1f} · 기준 {b:.1f}"),
    ("assists", "어시스트", (1.30, "어시스트 머신", "🤝", "good"), None, "경기당 어시스트 {v:.1f} · 기준 {b:.1f}"),
    ("kda", "KDA", (1.35, "높은 KDA", "🎯", "good"), (0.65, "낮은 KDA", "😵", "bad"), "평균 KDA {v:.2f} · 기준 {b:.2f}"),
    ("dpm", "딜량", (1.20, "높은 딜량", "💥", "good"), (0.75, "아쉬운 딜량", "🍃", "bad"), "분당 데미지 {v:.0f} · 기준 {b:.0f}"),
    ("dmg_taken", "탱킹", (1.30, "최전선 탱킹", "🧲", "neutral"), None, "경기당 받은 피해 {v:,.0f} · 기준 {b:,.0f}"),
    ("cspm", "CS", (1.20, "CS 머신", "🌾", "good"), None, "분당 CS {v:.1f} · 기준 {b:.1f}"),
    ("gpm", "골드", (1.15, "골드 수급 우수", "💰", "good"), None, "분당 골드 {v:.0f} · 기준 {b:.0f}"),
    ("wards", "시야", (1.35, "시야 장인", "👁️", "good"), (0.60, "시야 소홀", "🕯️", "bad"), "경기당 와드 {v:.1f} · 기준 {b:.1f}"),
    ("kp", "한타 참여", (1.20, "한타 본능", "🫂", "good"), (0.70, "마이웨이 플레이", "🐺", "neutral"), "킬 관여율 {v:.0%} · 기준 {b:.0%}"),
    ("ai", "AI점수", (1.12, "AI 고평가", "🧠", "good"), (0.85, "AI 혹평", "😓", "bad"), "평균 AI점수 {v:.2f} · 기준 {b:.2f}"),
]

# 신호가 부족할 때 3개를 채우는 중립 태그 (순서대로 사용).
_FALLBACK_TAGS = [
    {"label": "밸런스형", "emoji": "⚖️", "kind": "neutral", "reason": "주요 지표가 리그 평균에 고르게 근접"},
    {"label": "성장 중", "emoji": "🌿", "kind": "neutral", "reason": "데이터가 쌓이면 태그가 더 구체화됩니다"},
    {"label": "관찰 대상", "emoji": "🔍", "kind": "neutral", "reason": "두드러진 강점·약점이 아직 없습니다"},
]


def build_baselines(df_participants: pd.DataFrame) -> dict[str, Any]:
    """전체 참가 기록에서 파생 지표와 포지션별 리그 평균을 만든다.

    미연동 참가자(player_id 없음) 기록도 리그 평균 표본에는 포함해
    기준의 표본을 최대한 확보한다.
    """
    d = df_participants.copy()
    dur = pd.to_numeric(d["game_duration"], errors="coerce").replace(0, pd.NA) / 60
    d["dpm"] = pd.to_numeric(d["total_damage_dealt"], errors="coerce") / dur
    d["cspm"] = pd.to_numeric(d["cs"], errors="coerce") / dur
    d["gpm"] = pd.to_numeric(d["gold_earned"], errors="coerce") / dur
    d["wards"] = d["wards_placed"].fillna(0) + d["control_wards_bought"].fillna(0)
    d["dmg_taken"] = pd.to_numeric(d["total_damage_taken"], errors="coerce")
    d["ai"] = pd.to_numeric(d["ai_score"], errors="coerce")
    # 멀티킬 지수 — 상위 멀티킬일수록 가중.
    d["mk"] = (
        d["double_kills"].fillna(0)
        + 2 * d["triple_kills"].fillna(0)
        + 3 * d["quadra_kills"].fillna(0)
        + 4 * d["penta_kills"].fillna(0)
    )
    team_kills = d.groupby(["match_id", "team_id"])["kills"].sum().rename("team_kills")
    d = d.merge(team_kills, left_on=["match_id", "team_id"], right_index=True, how="left")
    d["kp"] = ((d["kills"] + d["assists"]) / d["team_kills"]).where(d["team_kills"] > 0)

    overall = d[_METRICS].mean()
    known = d[d["position"].isin(CANONICAL_POSITIONS)]
    pos_means = known.groupby("position")[_METRICS].mean()
    # 특정 포지션에 표본이 없는 지표는 전체 평균으로 보강.
    pos_means = pos_means.fillna(overall)

    # 기복 판정 기준 — AI점수 표준편차의 리그 중간값(5판 이상 플레이어).
    # AI점수는 0~100 스케일이라 절대 임계값 대신 리그 상대 비교를 쓴다.
    linked = d[d["player_id"].notna()]
    per_player = linked.groupby("player_id")["ai"].agg(["std", "count"])
    qualified_std = per_player[per_player["count"] >= 5]["std"].dropna()
    ai_std_ref = float(qualified_std.median()) if not qualified_std.empty else None

    return {"prepared": d, "pos_means": pos_means, "overall": overall, "ai_std_ref": ai_std_ref}


def _weighted_baseline(metric: str, pos_counts: pd.Series, pos_means: pd.DataFrame, overall: pd.Series):
    """플레이어의 포지션 구성비로 가중한 리그 기준값."""
    if pos_counts.empty:
        return overall.get(metric)
    total = pos_counts.sum()
    return sum(pos_means.loc[pos, metric] * cnt for pos, cnt in pos_counts.items()) / total


def compute_tags(player_id: int, baselines: dict[str, Any], *, max_tags: int = 3) -> list[dict[str, str]]:
    """플레이어의 자동 태그 top-N을 계산한다 (근거 포함, 점수 내림차순)."""
    d = baselines["prepared"]
    rows = d[d["player_id"] == player_id].sort_values("game_creation", ascending=False)
    games = len(rows)
    if games == 0:
        return [{"label": "기록 없음", "emoji": "📋", "kind": "neutral", "reason": "내전 경기 기록이 없습니다"}]

    pos_means, overall = baselines["pos_means"], baselines["overall"]
    pos_counts = rows["position"].value_counts()
    pos_counts = pos_counts[pos_counts.index.isin(pos_means.index)]

    cands: list[dict[str, Any]] = []

    # ── 포지션 가중 리그 평균 대비 비율 태그.
    # 임계값 미달이어도 편차 5% 이상이면 낮은 점수의 중립 태그(평균 상회/하회)로 남겨,
    # 두드러진 신호가 없는 평범한 플레이어도 실제 지표 기반 태그 3개를 받게 한다.
    for metric, noun, hi, lo, fmt in _RATIO_SPECS:
        mine = rows[metric].mean(skipna=True)
        base = _weighted_baseline(metric, pos_counts, pos_means, overall)
        if pd.isna(mine) or base is None or pd.isna(base) or float(base) <= 0:
            continue
        ratio = float(mine) / float(base)
        reason = fmt.format(v=float(mine), b=float(base))
        if ratio >= hi[0]:
            cands.append({"label": hi[1], "emoji": hi[2], "kind": hi[3], "reason": reason, "score": ratio / hi[0]})
        elif lo and 0 < ratio <= lo[0]:
            cands.append({"label": lo[1], "emoji": lo[2], "kind": lo[3], "reason": reason, "score": lo[0] / max(ratio, 0.05)})
        elif ratio >= 1.05:
            cands.append({"label": f"{noun} 평균 상회", "emoji": "🔼", "kind": "neutral",
                          "reason": reason, "score": ratio - 1.0})
        elif ratio <= 0.95:
            cands.append({"label": f"{noun} 평균 하회", "emoji": "🔽", "kind": "neutral",
                          "reason": reason, "score": 1.0 - ratio})

    # ── 연승/연패 — 최근 경기부터 같은 결과가 이어진 길이. 우선순위 최상위.
    wins = [bool(w) for w in rows["win"]]
    streak = 0
    for w in wins:
        if w == wins[0]:
            streak += 1
        else:
            break
    if streak >= 3:
        if wins[0]:
            cands.append({"label": "연승 중", "emoji": "🔥", "kind": "good",
                          "reason": f"최근 {streak}연승 중", "score": 2.0 + 0.1 * streak})
        else:
            cands.append({"label": "연패 중", "emoji": "🧊", "kind": "bad",
                          "reason": f"최근 {streak}연패 중", "score": 2.0 + 0.1 * streak})

    overall_wr = sum(wins) / games
    if games >= 8:
        # 폼 추세 — 최근 5판 승률이 전체 승률과 크게 벌어졌는지.
        recent_wr = sum(wins[:5]) / 5
        diff = recent_wr - overall_wr
        if diff >= 0.25:
            cands.append({"label": "상승세", "emoji": "📈", "kind": "good",
                          "reason": f"최근 5판 승률 {recent_wr:.0%} (전체 {overall_wr:.0%})", "score": 1.2 + diff})
        elif diff <= -0.25:
            cands.append({"label": "하락세", "emoji": "📉", "kind": "bad",
                          "reason": f"최근 5판 승률 {recent_wr:.0%} (전체 {overall_wr:.0%})", "score": 1.2 - diff})

        if overall_wr >= 0.60:
            cands.append({"label": "승리 보증수표", "emoji": "🏆", "kind": "good",
                          "reason": f"전체 승률 {overall_wr:.0%} ({games}판)", "score": 1.15 + (overall_wr - 0.60)})
        elif overall_wr <= 0.38:
            cands.append({"label": "승률 고전", "emoji": "🌧️", "kind": "bad",
                          "reason": f"전체 승률 {overall_wr:.0%} ({games}판)", "score": 1.15 + (0.38 - overall_wr)})

    # ── 챔피언 풀
    if games >= 6:
        champ_counts = rows["champion_name"].value_counts()
        top_share = champ_counts.iloc[0] / games
        uniq = len(champ_counts)
        if top_share >= 0.45:
            cands.append({"label": "원챔 장인", "emoji": "🎭", "kind": "neutral",
                          "reason": f"{champ_counts.index[0]} {top_share:.0%} 픽", "score": 1.0 + top_share})
        elif uniq / games >= 0.8:
            cands.append({"label": "넓은 챔프폭", "emoji": "🎨", "kind": "neutral",
                          "reason": f"{games}판 동안 {uniq}개 챔피언", "score": 1.0 + uniq / games - 0.8})

    # ── 멀티킬
    league_mk = overall.get("mk")
    if games >= 5 and league_mk is not None and pd.notna(league_mk) and league_mk > 0:
        ratio = rows["mk"].mean() / league_mk
        raw_ct = int(
            (rows["double_kills"] + rows["triple_kills"] + rows["quadra_kills"] + rows["penta_kills"]).sum()
        )
        if ratio >= 1.6 and raw_ct >= 2:
            cands.append({"label": "멀티킬 메이커", "emoji": "⚡", "kind": "good",
                          "reason": f"멀티킬 {raw_ct}회 ({games}판)", "score": min(ratio / 1.6, 1.8)})

    # ── 경기력 기복 — AI점수 표준편차를 리그 중간값과 비교 (스케일 독립적).
    ai_std_ref = baselines.get("ai_std_ref")
    if games >= 5 and ai_std_ref:
        std = rows["ai"].std()
        if pd.notna(std):
            ratio = float(std) / ai_std_ref
            if ratio <= 0.65:
                cands.append({"label": "기복 없음", "emoji": "🧱", "kind": "good",
                              "reason": f"AI점수 표준편차 {std:.1f} (리그 중간값 {ai_std_ref:.1f}) — 꾸준한 경기력",
                              "score": 1.05 + (0.65 - ratio) / 2})
            elif ratio >= 1.35:
                cands.append({"label": "기복 롤러코스터", "emoji": "🎢", "kind": "neutral",
                              "reason": f"AI점수 표준편차 {std:.1f} (리그 중간값 {ai_std_ref:.1f}) — 경기별 편차 큼",
                              "score": 1.0 + (ratio - 1.35) / 2})

    cands.sort(key=lambda t: t["score"], reverse=True)

    tags: list[dict[str, str]] = []
    if games < 5:
        tags.append({"label": "표본 적음", "emoji": "🌱", "kind": "neutral",
                     "reason": f"{games}판 — 지표 신뢰도가 낮습니다"})
    for c in cands:
        if len(tags) >= max_tags:
            break
        if any(t["label"] == c["label"] for t in tags):
            continue
        tags.append({k: c[k] for k in ("label", "emoji", "kind", "reason")})
    for fb in _FALLBACK_TAGS:
        if len(tags) >= max_tags:
            break
        if not any(t["label"] == fb["label"] for t in tags):
            tags.append(dict(fb))
    return tags[:max_tags]
