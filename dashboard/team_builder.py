"""내전 팀 밸런스 시뮬레이터 — 대시보드 전용 페이지.

등록 플레이어 중 10명을 체크박스로 고르면 C(9,4)=126개 분할을 전수 평가해
선택한 밸런스 기준(종합/MMR/주라인/팀합/플레이스타일/최근폼)으로 가장 균형 잡힌
1팀/2팀을 구성한다. 상위 3개 조합을 1안/2안/3안으로 함께 제시하고, 각 팀의
추천 라인·모스트 챔피언 TOP3·팀 지표 비교(레이더/표)·선정 이유를 보여준다.

app.py에서 importlib으로 로드된다. 아이콘 헬퍼·차트 설정 등 앱 공용 자원은
render_team_builder(ctx)의 ctx 딕셔너리로 주입받는다 (순환 의존 방지).
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

LANES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
TEAM_SIZE = 5
PICK_COUNT = 10

BLUE = "#63B3ED"
RED = "#FC8181"

# 밸런스 기준 정의 — penalty: 분할 페널티 가중치, score: 종합 점수 가중치(합=1).
# 페널티 구성요소는 _evaluate_splits()에서 계산하는 P_* 값들이다.
MODES: dict[str, dict[str, Any]] = {
    "balanced": {
        "label": "⚖️ 종합 밸런스",
        "desc": "MMR·라인·팀합·스타일·폼을 모두 반영한 올라운드 밸런스",
        "penalty": {"mmr": 5.0, "fit": 1.5, "fit_gap": 0.5, "syn_gap": 0.8, "style_gap": 0.7, "form_gap": 0.8},
        "score": {"mmr": 0.30, "lane": 0.20, "synergy": 0.18, "style": 0.16, "form": 0.16},
    },
    "mmr": {
        "label": "🎯 MMR 우선",
        "desc": "두 팀의 평균 MMR 차이를 최소화 (실력 균형이 최우선)",
        "penalty": {"mmr": 10.0, "fit": 0.3, "style_gap": 0.1},
        "score": {"mmr": 0.55, "lane": 0.15, "synergy": 0.10, "style": 0.10, "form": 0.10},
    },
    "lane": {
        "label": "🛡️ 주라인 우선",
        "desc": "모든 플레이어가 주/부 포지션에 서도록 라인 적합도를 최대화",
        "penalty": {"mmr": 1.5, "fit": 4.0, "fit_gap": 2.0},
        "score": {"mmr": 0.20, "lane": 0.50, "synergy": 0.10, "style": 0.10, "form": 0.10},
    },
    "synergy": {
        "label": "🤝 팀합 우선",
        "desc": "실제 내전 듀오 승률 데이터를 기반으로 양 팀 시너지를 균등하게",
        "penalty": {"mmr": 1.5, "fit": 0.5, "syn_gap": 3.0, "syn_total": 1.0},
        "score": {"mmr": 0.20, "lane": 0.10, "synergy": 0.50, "style": 0.10, "form": 0.10},
    },
    "style": {
        "label": "🎭 플레이스타일 우선",
        "desc": "캐리력·안정성·한타·시야 성향이 양 팀에 고르게 분산되도록",
        "penalty": {"mmr": 1.5, "fit": 0.5, "style_gap": 4.0},
        "score": {"mmr": 0.20, "lane": 0.10, "synergy": 0.10, "style": 0.50, "form": 0.10},
    },
    "form": {
        "label": "🔥 최근 폼 우선",
        "desc": "최근 10판 승률·AI점수 기준 상승세/하락세를 균등 분배",
        "penalty": {"mmr": 1.5, "fit": 0.5, "form_gap": 4.0},
        "score": {"mmr": 0.20, "lane": 0.10, "synergy": 0.10, "style": 0.10, "form": 0.50},
    },
}

SCORE_COMPONENT_LABELS = {
    "mmr": "⚖️ MMR 균형",
    "lane": "🛡️ 라인 적합도",
    "synergy": "🤝 시너지 균형",
    "style": "🎭 스타일 균형",
    "form": "🔥 폼 균형",
}

STYLE_CATEGORIES = ["캐리력", "안정성", "한타 참여", "시야 장악", "폼"]

_CSS = """
<style>
.tb-count-bar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:6px 0 14px; }
.tb-count {
    font-size:1.05rem; font-weight:800; padding:7px 16px; border-radius:999px;
    background:rgba(102,252,241,0.10); border:1px solid rgba(102,252,241,0.45); color:#9EE9E4;
}
.tb-count.full { background:rgba(16,185,129,0.14); border-color:rgba(16,185,129,0.55); color:#34D399; }
.tb-count.over { background:rgba(239,68,68,0.14); border-color:rgba(239,68,68,0.55); color:#F87171; }
.tb-pos-chip {
    font-size:0.78rem; font-weight:700; padding:4px 11px; border-radius:999px;
    background:rgba(31,40,51,0.85); border:1px solid rgba(69,162,158,0.4); color:#C5C6C7;
}
.tb-pos-chip.missing { border-color:rgba(239,68,68,0.5); color:#F87171; }

.tb-team-head {
    display:flex; align-items:center; justify-content:space-between; gap:10px;
    padding:12px 16px; border-radius:12px; margin-bottom:10px;
}
.tb-team-head.blue { background:linear-gradient(90deg, rgba(26,54,93,0.95), rgba(26,54,93,0.25)); border-left:4px solid #63B3ED; }
.tb-team-head.red  { background:linear-gradient(90deg, rgba(116,42,42,0.95), rgba(116,42,42,0.25)); border-left:4px solid #FC8181; }
.tb-team-head .t-name { font-size:1.1rem; font-weight:800; }
.tb-team-head.blue .t-name { color:#90CDF4; }
.tb-team-head.red .t-name { color:#FEB2B2; }
.tb-team-head .t-sub { font-size:0.78rem; color:#9AA7AE; margin-top:2px; }
.tb-team-head .t-prob { text-align:right; }
.tb-team-head .t-prob .v { font-size:1.35rem; font-weight:800; color:#E9FEFC; line-height:1.1; }
.tb-team-head .t-prob .k { font-size:0.68rem; color:#9AA7AE; }

.tb-card {
    background:linear-gradient(150deg, rgba(31,40,51,0.92), rgba(18,24,31,0.94));
    border:1px solid rgba(69,162,158,0.30); border-radius:13px;
    padding:12px 14px; margin-bottom:10px; transition:border-color .15s ease, transform .15s ease;
}
.tb-card:hover { border-color:rgba(102,252,241,0.55); transform:translateY(-1px); }
.tb-card.blue { border-left:4px solid #63B3ED; }
.tb-card.red  { border-left:4px solid #FC8181; }
.tb-row1 { display:flex; align-items:center; gap:10px; }
.tb-row1 img.lane { width:26px; height:26px; }
.tb-lane-name { font-size:0.8rem; font-weight:800; color:#9EE9E4; min-width:34px; }
.tb-name { font-size:1.02rem; font-weight:800; color:#E9FEFC; }
.tb-nick { font-size:0.75rem; color:#7C8A92; font-weight:600; margin-left:6px; }
.tb-mmr { margin-left:auto; text-align:right; }
.tb-mmr .v { font-size:1.15rem; font-weight:800; color:#66FCF1; line-height:1.1; }
.tb-mmr .k { font-size:0.62rem; color:#7C8A92; letter-spacing:0.5px; }
.tb-fit {
    font-size:0.68rem; font-weight:800; padding:2px 8px; border-radius:999px; white-space:nowrap;
}
.tb-fit.main { color:#34D399; background:rgba(16,185,129,0.12); border:1px solid rgba(16,185,129,0.45); }
.tb-fit.sub  { color:#FFD86B; background:rgba(255,215,0,0.10); border:1px solid rgba(255,215,0,0.40); }
.tb-fit.etc  { color:#94A3B8; background:rgba(148,163,184,0.10); border:1px solid rgba(148,163,184,0.35); }
.tb-fit.none { color:#F87171; background:rgba(239,68,68,0.10); border:1px solid rgba(239,68,68,0.40); }
.tb-row2 { display:flex; align-items:center; gap:8px; margin-top:8px; flex-wrap:wrap; }
.tb-row2 img.tier { width:22px; height:22px; }
.tb-stat { font-size:0.74rem; color:#9AA7AE; font-weight:600; }
.tb-stat b { color:#C5C6C7; font-weight:800; }
.tb-form-w { color:#34D399; font-weight:800; }
.tb-form-l { color:#F87171; font-weight:800; }
.tb-tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.tb-tag {
    font-size:0.68rem; font-weight:800; padding:2px 9px; border-radius:999px;
    border:1px solid; cursor:help; white-space:nowrap;
}
.tb-tag.good { color:#34D399; background:rgba(16,185,129,0.10); border-color:rgba(16,185,129,0.45); }
.tb-tag.bad { color:#F87171; background:rgba(239,68,68,0.10); border-color:rgba(239,68,68,0.45); }
.tb-tag.neutral { color:#94A3B8; background:rgba(148,163,184,0.10); border-color:rgba(148,163,184,0.4); }
.tb-row3 { display:flex; gap:10px; margin-top:9px; }
.tb-champ { text-align:center; min-width:52px; }
.tb-champ img { width:38px; height:38px; border-radius:9px; border:1px solid rgba(102,252,241,0.30); }
.tb-champ .cg { font-size:0.66rem; color:#9AA7AE; font-weight:700; margin-top:2px; line-height:1.25; }
.tb-champ .cw { font-size:0.66rem; font-weight:800; }
.tb-champ .cw.hi { color:#34D399; } .tb-champ .cw.mid { color:#94A3B8; } .tb-champ .cw.lo { color:#F87171; }
.tb-champ-empty { font-size:0.74rem; color:#7C8A92; padding:10px 0; }

.tb-vs-row {
    display:grid; grid-template-columns: 1fr 74px 1fr; align-items:center; gap:8px;
    background:rgba(255,255,255,0.025); border-radius:10px; padding:8px 12px; margin-bottom:6px;
}
.tb-vs-side { display:flex; align-items:center; gap:8px; }
.tb-vs-side.right { justify-content:flex-end; text-align:right; }
.tb-vs-name { font-size:0.9rem; font-weight:800; color:#E9FEFC; }
.tb-vs-sub { font-size:0.7rem; color:#7C8A92; }
.tb-vs-mid { text-align:center; }
.tb-vs-mid img { width:24px; height:24px; }
.tb-vs-mid .l { font-size:0.66rem; color:#9EE9E4; font-weight:800; }
.tb-vs-diff { font-size:0.72rem; font-weight:800; }
.tb-vs-diff.blue { color:#90CDF4; } .tb-vs-diff.red { color:#FEB2B2; } .tb-vs-diff.even { color:#94A3B8; }

.tb-chip-bar { display:flex; flex-wrap:wrap; gap:8px; margin:2px 0 12px; }
.tb-chip {
    font-size:0.82rem; font-weight:700; padding:6px 13px; border-radius:999px;
    background:rgba(31,40,51,0.9); border:1px solid rgba(69,162,158,0.45); color:#C5C6C7;
}
.tb-chip b { color:#66FCF1; }
.tb-chip.gold { border-color:rgba(255,215,0,0.5); color:#FFD86B; background:rgba(255,215,0,0.08); }
.tb-chip.gold b { color:#FFD86B; }

.tb-reason { margin:0; padding-left:18px; }
.tb-reason li { font-size:0.88rem; color:#CFE0E2; line-height:1.65; margin-bottom:7px; }
.tb-reason li b { color:#9EE9E4; }
.tb-warn { color:#FFD86B; }
</style>
"""


# ══════════════════════════════════ 데이터 준비 ══════════════════════════════════

def _norm_map(values: dict[int, float]) -> dict[int, float]:
    """pid→값 딕셔너리를 0~1로 min-max 정규화. 전원이 같으면 0.5."""
    vals = list(values.values())
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return {pid: 0.5 for pid in values}
    return {pid: (v - lo) / (hi - lo) for pid, v in values.items()}


def build_profiles(pids: list[int], ctx: dict) -> dict[int, dict[str, Any]]:
    """선택된 플레이어들의 밸런싱/표시용 프로필을 구성한다."""
    dfp = ctx["df_players"].set_index("id")
    parts = ctx["df_participants"]
    ps = ctx["position_service"]

    team_kills = parts.groupby(["match_id", "team_id"])["kills"].sum()
    tagger = ctx.get("get_player_tags")

    profiles: dict[int, dict[str, Any]] = {}
    for pid in pids:
        row = dfp.loc[pid]
        p = parts[parts["player_id"] == pid].sort_values("game_creation", ascending=False)

        positions = [x for x in p["position"].tolist() if x and x != "UNKNOWN"]
        ranked = ps.rank_positions(positions)
        lane_scores: dict[str, float] = {}
        if ranked:
            top_score = ranked[0][1]
            lane_scores = {lane: score / top_score for lane, score in ranked}

        champs: list[dict[str, Any]] = []
        if not p.empty:
            g = p.groupby("champion_name").agg(
                games=("champion_name", "size"), wins=("win", "sum"), kda=("kda", "mean")
            )
            g["wr"] = g["wins"] / g["games"] * 100
            g = g.sort_values(["games", "wr"], ascending=[False, False]).head(3)
            champs = [
                {"name": name, "games": int(r["games"]), "wr": float(r["wr"]), "kda": float(r["kda"])}
                for name, r in g.iterrows()
            ]

        last5 = ["W" if bool(w) else "L" for w in p["win"].head(5)]
        recent10 = p["win"].head(10)
        recent_wr = float(recent10.mean()) if len(recent10) else 0.5

        dur_min = p["game_duration"].replace(0, pd.NA) / 60
        dpm_series = (p["total_damage_dealt"] / dur_min).dropna()
        dpm = float(dpm_series.mean()) if not dpm_series.empty else 0.0

        kp_vals = []
        for _, r in p.iterrows():
            tk = team_kills.get((r["match_id"], r["team_id"]), 0)
            if tk and tk > 0:
                kp_vals.append((r["kills"] + r["assists"]) / tk)
        kp = float(sum(kp_vals) / len(kp_vals)) if kp_vals else 0.0

        games = int(row["matches_played"])
        avg_ai = float(row["avg_ai_score"]) if games else 0.0
        nick = str(row.get("riot_accounts") or "-").split(",")[0].strip()

        profiles[pid] = {
            "pid": pid,
            "name": str(row["display_name"]),
            "nick": nick,
            "mmr": int(row["mmr"]),
            "tier": row.get("current_tier"),
            "tier_label": str(row.get("current_tier_label") or "-"),
            "main_pos": str(row["main_pos"]),
            "sub_pos": str(row["sub_pos"]),
            "games": games,
            "win_rate": float(row["win_rate"]),
            "avg_kda": float(row["avg_kda"]),
            "avg_ai": avg_ai,
            "avg_kills": float(row["avg_kills"]),
            "avg_deaths": float(row["avg_deaths"]),
            "avg_wards": float(row["avg_wards_placed"]) + float(row["avg_control_wards"]),
            "lane_scores": lane_scores,
            "champs": champs,
            "last5": last5,
            "recent_wr": recent_wr,
            "dpm": dpm,
            "kp": kp,
            # 폼 지수 0~1 — 최근 10판 승률 70% + 평균 AI점수 30% (AI점수는 0~100 스케일)
            "form": 0.7 * recent_wr + 0.3 * min(max(avg_ai / 100.0, 0.0), 1.0),
            "tags": tagger(pid) if tagger else [],
        }

    # 스타일 벡터 — 선택된 10명 안에서 상대 정규화해 5개 성향 점수(0~1)를 만든다.
    n_dpm = _norm_map({pid: pr["dpm"] for pid, pr in profiles.items()})
    n_kills = _norm_map({pid: pr["avg_kills"] for pid, pr in profiles.items()})
    n_kda = _norm_map({pid: pr["avg_kda"] for pid, pr in profiles.items()})
    n_deaths = _norm_map({pid: pr["avg_deaths"] for pid, pr in profiles.items()})
    n_kp = _norm_map({pid: pr["kp"] for pid, pr in profiles.items()})
    n_wards = _norm_map({pid: pr["avg_wards"] for pid, pr in profiles.items()})
    n_form = _norm_map({pid: pr["form"] for pid, pr in profiles.items()})
    for pid, pr in profiles.items():
        pr["style_vec"] = {
            "캐리력": 0.6 * n_dpm[pid] + 0.4 * n_kills[pid],
            "안정성": 0.5 * n_kda[pid] + 0.5 * (1 - n_deaths[pid]),
            "한타 참여": n_kp[pid],
            "시야 장악": n_wards[pid],
            "폼": n_form[pid],
        }
    return profiles


def build_pair_stats(pids: list[int], parts: pd.DataFrame) -> dict[tuple[int, int], dict[str, Any]]:
    """선택된 플레이어끼리 같은 팀으로 뛴 (판수, 승수) 듀오 전적."""
    sel = parts[parts["player_id"].isin(pids)]
    raw: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    for (_, _), grp in sel.groupby(["match_id", "team_id"]):
        dedup = grp.drop_duplicates("player_id")
        ids = sorted(int(x) for x in dedup["player_id"].tolist())
        if len(ids) < 2:
            continue
        win = bool(dedup["win"].iloc[0])
        for a, b in itertools.combinations(ids, 2):
            raw[(a, b)][0] += 1
            raw[(a, b)][1] += 1 if win else 0
    return {
        k: {"games": g, "wins": w, "wr": w / g}
        for k, (g, w) in raw.items()
    }


# ══════════════════════════════════ 밸런싱 계산 ══════════════════════════════════

def _assign_lanes(team: list[int], profiles: dict) -> tuple[dict[str, int], float]:
    """5명을 5개 라인에 배정 — 최신 가중 포지션 적합도 합이 최대가 되는 순열을 고른다."""
    best_perm, best_fit = None, -1.0
    for perm in itertools.permutations(team):
        fit = sum(
            profiles[pid]["lane_scores"].get(lane, 0.0)
            for lane, pid in zip(LANES, perm)
        )
        if fit > best_fit:
            best_fit, best_perm = fit, perm
    return dict(zip(LANES, best_perm)), best_fit


def _team_synergy(team: list[int], pair_stats: dict) -> float:
    """팀 내 듀오 전적 기반 시너지 점수. 승률 50% 기준 편차를 판수 가중으로 합산."""
    total = 0.0
    for a, b in itertools.combinations(sorted(team), 2):
        rec = pair_stats.get((a, b))
        if rec:
            total += (rec["wr"] - 0.5) * min(rec["games"], 8) / 8
    return total


def _evaluate_splits(pids: list[int], profiles: dict, pair_stats: dict) -> list[dict[str, Any]]:
    """가능한 모든 팀 분할(126개)의 밸런스 구성요소를 계산한다."""
    first, rest = pids[0], pids[1:]
    results = []
    for comb in itertools.combinations(rest, 4):
        t1 = [first, *comb]
        t1_set = set(t1)
        t2 = [p for p in pids if p not in t1_set]

        m1 = sum(profiles[p]["mmr"] for p in t1) / TEAM_SIZE
        m2 = sum(profiles[p]["mmr"] for p in t2) / TEAM_SIZE
        mmr_diff = abs(m1 - m2)

        assign1, fit1 = _assign_lanes(t1, profiles)
        assign2, fit2 = _assign_lanes(t2, profiles)

        syn1 = _team_synergy(t1, pair_stats)
        syn2 = _team_synergy(t2, pair_stats)

        style_gap = sum(
            abs(
                sum(profiles[p]["style_vec"][cat] for p in t1)
                - sum(profiles[p]["style_vec"][cat] for p in t2)
            )
            for cat in STYLE_CATEGORIES
        )

        f1 = sum(profiles[p]["form"] for p in t1) / TEAM_SIZE
        f2 = sum(profiles[p]["form"] for p in t2) / TEAM_SIZE

        results.append({
            "t1": t1, "t2": t2,
            "assign1": assign1, "assign2": assign2,
            "mmr1": m1, "mmr2": m2, "mmr_diff": mmr_diff,
            "fit1": fit1, "fit2": fit2,
            "syn1": syn1, "syn2": syn2,
            "style_gap": style_gap,
            "form1": f1, "form2": f2,
            "p_blue": 1.0 / (1.0 + 10 ** ((m2 - m1) / 400)),
        })
    return results


def _penalty(c: dict, weights: dict[str, float]) -> float:
    """분할 구성요소 → 단일 페널티.

    각 항의 스케일 계수는 실데이터 126개 분할에서 항별 값 분포(spread)가
    MMR 차이(0~450 수준)와 비슷해지도록 보정한 값이다. 스케일이 맞아야
    모드별 가중치가 의도대로 우선순위를 바꾼다.
    """
    terms = {
        "mmr": c["mmr_diff"],
        "fit": (2 * TEAM_SIZE - (c["fit1"] + c["fit2"])) * 100,
        "fit_gap": abs(c["fit1"] - c["fit2"]) * 100,
        "syn_gap": abs(c["syn1"] - c["syn2"]) * 250,
        "syn_total": -(c["syn1"] + c["syn2"]) * 200,
        "style_gap": c["style_gap"] * 100,
        "form_gap": abs(c["form1"] - c["form2"]) * 2000,
    }
    return sum(terms[k] * w for k, w in weights.items())


def _component_scores(c: dict) -> dict[str, float]:
    """구성요소 → 0~100 점수 (표시용). 계수는 실데이터 분포 기준으로,
    최상 분할이 90점대·최악 분할이 0~20점대에 오도록 맞췄다."""
    return {
        "mmr": max(0.0, 100 - c["mmr_diff"] / 3),
        "lane": (c["fit1"] + c["fit2"]) / (2 * TEAM_SIZE) * 100,
        "synergy": max(0.0, 100 - abs(c["syn1"] - c["syn2"]) * 60),
        "style": max(0.0, 100 - c["style_gap"] * 20),
        "form": max(0.0, 100 - abs(c["form1"] - c["form2"]) * 350),
    }


def generate_plans(pids: list[int], mode_key: str, ctx: dict) -> dict[str, Any]:
    """상위 3개 분할(1안/2안/3안)과 프로필·듀오 전적을 계산해 반환한다."""
    profiles = build_profiles(pids, ctx)
    pair_stats = build_pair_stats(pids, ctx["df_participants"])
    splits = _evaluate_splits(pids, profiles, pair_stats)

    mode = MODES[mode_key]
    splits.sort(key=lambda c: _penalty(c, mode["penalty"]))

    plans = []
    for c in splits[:3]:
        scores = _component_scores(c)
        overall = sum(scores[k] * w for k, w in mode["score"].items())
        plans.append({**c, "scores": scores, "overall": overall})
    return {"plans": plans, "profiles": profiles, "pair_stats": pair_stats}


# ══════════════════════════════════ 설명 생성 ══════════════════════════════════

def _lane_fit_kind(profile: dict, lane: str) -> tuple[str, str]:
    """(칩 클래스, 라벨) — 배정 라인이 주/부/경험/자동 중 무엇인지."""
    if lane == profile["main_pos"]:
        return "main", "주 포지션"
    if lane == profile["sub_pos"]:
        return "sub", "부 포지션"
    share = profile["lane_scores"].get(lane, 0.0)
    if share > 0:
        return "etc", f"경험 라인 ({share * 100:.0f}%)"
    if not profile["lane_scores"]:
        return "none", "표본 없음 · 자동"
    return "none", "자동 배정"


def _best_duo(team: list[int], pair_stats: dict, profiles: dict, *, min_games: int = 2):
    """팀 내 표본 있는 듀오 중 최고 승률 (판수 min_games 이상)."""
    best = None
    for a, b in itertools.combinations(sorted(team), 2):
        rec = pair_stats.get((a, b))
        if rec and rec["games"] >= min_games:
            if best is None or rec["wr"] > best[2]["wr"]:
                best = (a, b, rec)
    if not best:
        return None
    a, b, rec = best
    return {"names": f"{profiles[a]['name']}+{profiles[b]['name']}", **rec}


def build_reasons(plan: dict, mode_key: str, profiles: dict, pair_stats: dict) -> list[str]:
    """이 조합이 선정된 이유를 사람이 읽는 문장으로 만든다."""
    mode = MODES[mode_key]
    reasons = [
        f"<b>{mode['label']}</b> 기준 — {mode['desc']}. 가능한 <b>126개 조합 전수</b> 중 "
        f"페널티가 가장 낮은 조합을 선택했습니다."
    ]

    p_blue = plan["p_blue"] * 100
    reasons.append(
        f"평균 MMR <b>블루 {plan['mmr1']:.0f}</b> vs <b>레드 {plan['mmr2']:.0f}</b> — "
        f"차이 <b>{plan['mmr_diff']:.0f}점</b>, Elo 기대 승률 <b>{p_blue:.0f}% : {100 - p_blue:.0f}%</b>."
    )

    # 에이스 분산 — MMR 상위 2명이 어떻게 나뉘었는지.
    top2 = sorted(profiles.values(), key=lambda p: p["mmr"], reverse=True)[:2]
    a, b = top2[0], top2[1]
    t1_set = set(plan["t1"])
    if (a["pid"] in t1_set) != (b["pid"] in t1_set):
        reasons.append(
            f"MMR 1·2위 <b>{a['name']}({a['mmr']})</b>·<b>{b['name']}({b['mmr']})</b>를 "
            f"서로 다른 팀에 배치해 에이스를 분산했습니다."
        )
    else:
        team_label = "블루" if a["pid"] in t1_set else "레드"
        reasons.append(
            f"MMR 1·2위 <b>{a['name']}</b>·<b>{b['name']}</b>가 같은 {team_label}팀이지만, "
            f"나머지 인원 구성으로 평균 MMR 차이를 {plan['mmr_diff']:.0f}점까지 상쇄했습니다."
        )

    # 라인 적합도.
    fit_counts = {"main": 0, "sub": 0, "etc": 0, "none": 0}
    for assign in (plan["assign1"], plan["assign2"]):
        for lane, pid in assign.items():
            kind, _ = _lane_fit_kind(profiles[pid], lane)
            fit_counts[kind] += 1
    lane_txt = (
        f"라인 적합도 <b>블루 {plan['fit1']:.1f}/5 · 레드 {plan['fit2']:.1f}/5</b> — "
        f"주 포지션 배정 <b>{fit_counts['main']}명</b>, 부 포지션 <b>{fit_counts['sub']}명</b>"
    )
    if fit_counts["etc"]:
        lane_txt += f", 경험 라인 {fit_counts['etc']}명"
    if fit_counts["none"]:
        lane_txt += f", <span class='tb-warn'>자동 배정 {fit_counts['none']}명</span>"
    reasons.append(lane_txt + ".")

    # 팀합 — 실제 듀오 전적.
    duo1 = _best_duo(plan["t1"], pair_stats, profiles)
    duo2 = _best_duo(plan["t2"], pair_stats, profiles)
    if duo1 or duo2:
        parts_txt = []
        if duo1:
            parts_txt.append(f"블루 <b>{duo1['names']}</b> 승률 {duo1['wr'] * 100:.0f}%({duo1['games']}판)")
        if duo2:
            parts_txt.append(f"레드 <b>{duo2['names']}</b> 승률 {duo2['wr'] * 100:.0f}%({duo2['games']}판)")
        reasons.append(
            "팀 내 핵심 듀오 — " + " · ".join(parts_txt)
            + f". 시너지 지수 격차 {abs(plan['syn1'] - plan['syn2']):.2f} (0에 가까울수록 균형)."
        )
    else:
        reasons.append("함께 뛴 표본이 있는 듀오가 없어 팀합은 중립으로 평가했습니다.")

    # 스타일 분산.
    cat_txts = []
    for cat in ("캐리력", "안정성"):
        s1 = sum(profiles[p]["style_vec"][cat] for p in plan["t1"])
        s2 = sum(profiles[p]["style_vec"][cat] for p in plan["t2"])
        cat_txts.append(f"{cat} {s1:.1f}:{s2:.1f}")
    reasons.append(
        f"플레이스타일 분포 — {' · '.join(cat_txts)} (팀 합산). "
        f"5개 성향 총 격차 <b>{plan['style_gap']:.2f}</b> (낮을수록 고른 분산)."
    )

    # 최근 폼.
    reasons.append(
        f"최근 폼 지수(최근 10판 승률 70% + AI점수 30%) — "
        f"블루 <b>{plan['form1'] * 100:.0f}</b> vs 레드 <b>{plan['form2'] * 100:.0f}</b>."
    )

    # 표본 없는 플레이어 경고.
    no_data = [p["name"] for p in profiles.values() if p["games"] == 0]
    if no_data:
        reasons.append(
            f"<span class='tb-warn'>⚠️ {', '.join(no_data)}: 내전 기록이 없어 "
            f"MMR(티어 환산)만 반영되고 라인은 남는 자리에 자동 배정됩니다.</span>"
        )
    return reasons


# ══════════════════════════════════ 렌더링 ══════════════════════════════════

def _fmt_last5(last5: list[str]) -> str:
    if not last5:
        return "<span class='tb-stat'>기록 없음</span>"
    return "".join(
        f"<span class='{'tb-form-w' if r == 'W' else 'tb-form-l'}'>{r}</span>" for r in last5
    )


def _wr_class(wr: float) -> str:
    if wr >= 55:
        return "hi"
    if wr >= 45:
        return "mid"
    return "lo"


def _player_card(profile: dict, lane: str, side: str, ctx: dict) -> str:
    pos_kr = ctx["position_service"].POSITION_KR
    lane_uri = ctx["position_icon"](lane)
    lane_img = f"<img class='lane' src='{lane_uri}'>" if lane_uri else "🎯"
    fit_cls, fit_label = _lane_fit_kind(profile, lane)
    tier_uri = ctx["tier_emblem"](profile["tier"])
    tier_img = f"<img class='tier' src='{tier_uri}'>" if tier_uri else ""

    if profile["champs"]:
        champ_cells = []
        for chp in profile["champs"]:
            uri = ctx["champ_icon_by_name"](chp["name"])
            img = f"<img src='{uri}' title='{chp['name']} · {chp['games']}판 · 승률 {chp['wr']:.0f}% · KDA {chp['kda']:.2f}'>" if uri else "🎮"
            champ_cells.append(
                f"<div class='tb-champ'>{img}"
                f"<div class='cg'>{chp['games']}판</div>"
                f"<div class='cw {_wr_class(chp['wr'])}'>{chp['wr']:.0f}%</div></div>"
            )
        champs_html = "".join(champ_cells)
    else:
        champs_html = "<div class='tb-champ-empty'>챔피언 기록 없음</div>"

    if profile["games"]:
        stats = (
            f"<span class='tb-stat'>AI <b>{profile['avg_ai']:.1f}</b></span>"
            f"<span class='tb-stat'>KDA <b>{profile['avg_kda']:.2f}</b></span>"
            f"<span class='tb-stat'>승률 <b>{profile['win_rate']:.0f}%</b></span>"
            f"<span class='tb-stat'>{profile['games']}판</span>"
            f"<span class='tb-stat'>최근 {_fmt_last5(profile['last5'])}</span>"
        )
    else:
        stats = "<span class='tb-stat'>내전 기록 없음 — 티어 환산 MMR만 반영</span>"

    tags_html = "".join(
        f"<span class='tb-tag {t['kind']}' title=\"{t['reason']}\">{t['emoji']} {t['label']}</span>"
        for t in profile.get("tags", [])
    )
    tags_row = f"<div class='tb-tags'>{tags_html}</div>" if tags_html else ""

    return (
        f"<div class='tb-card {side}'>"
        f"<div class='tb-row1'>{lane_img}<span class='tb-lane-name'>{pos_kr.get(lane, lane)}</span>"
        f"<span class='tb-name'>{profile['name']}<span class='tb-nick'>{profile['nick']}</span></span>"
        f"<span class='tb-fit {fit_cls}'>{fit_label}</span>"
        f"<div class='tb-mmr'><div class='v'>{profile['mmr']}</div><div class='k'>MMR</div></div></div>"
        f"<div class='tb-row2'>{tier_img}<span class='tb-stat'><b>{profile['tier_label']}</b></span>{stats}</div>"
        f"{tags_row}"
        f"<div class='tb-row3'>{champs_html}</div>"
        f"</div>"
    )


def _team_aggregate(team: list[int], profiles: dict) -> dict[str, float]:
    n = len(team)
    return {
        "MMR": sum(profiles[p]["mmr"] for p in team) / n,
        "캐리력 (DPM)": sum(profiles[p]["dpm"] for p in team) / n,
        "안정성 (KDA)": sum(profiles[p]["avg_kda"] for p in team) / n,
        "한타 참여 (KP%)": sum(profiles[p]["kp"] for p in team) / n * 100,
        "시야 (와드/판)": sum(profiles[p]["avg_wards"] for p in team) / n,
        "최근 폼 (승률%)": sum(profiles[p]["recent_wr"] for p in team) / n * 100,
        "평균 AI점수": sum(profiles[p]["avg_ai"] for p in team) / n,
    }


def _render_radar(agg1: dict, agg2: dict, ctx: dict):
    axes = ["MMR", "캐리력 (DPM)", "안정성 (KDA)", "한타 참여 (KP%)", "시야 (와드/판)", "최근 폼 (승률%)"]
    r1, r2 = [], []
    for ax in axes:
        peak = max(agg1[ax], agg2[ax], 1e-9)
        r1.append(agg1[ax] / peak * 100)
        r2.append(agg2[ax] / peak * 100)

    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=r1 + r1[:1], theta=axes + axes[:1], fill="toself", name="🔵 블루팀",
        line=dict(color=BLUE, width=2), fillcolor="rgba(99,179,237,0.18)",
    ))
    fig.add_trace(go.Scatterpolar(
        r=r2 + r2[:1], theta=axes + axes[:1], fill="toself", name="🔴 레드팀",
        line=dict(color=RED, width=2), fillcolor="rgba(252,129,129,0.18)",
    ))
    fig.update_layout(
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(visible=True, range=[0, 105], showticklabels=False, gridcolor="rgba(255,255,255,0.12)"),
            angularaxis=dict(gridcolor="rgba(255,255,255,0.12)", tickfont=dict(size=11)),
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="#FFFFFF",
        legend=dict(orientation="h", yanchor="bottom", y=-0.15, x=0.25),
        margin=dict(l=50, r=50, t=30, b=30),
        height=380,
    )
    ctx["freeze_chart"](fig)
    st.plotly_chart(fig, width="stretch", config=ctx["plotly_config"])


def _render_lane_matchups(plan: dict, profiles: dict, ctx: dict):
    pos_kr = ctx["position_service"].POSITION_KR
    rows = []
    for lane in LANES:
        p1 = profiles[plan["assign1"][lane]]
        p2 = profiles[plan["assign2"][lane]]
        diff = p1["mmr"] - p2["mmr"]
        if abs(diff) < 30:
            diff_html = "<span class='tb-vs-diff even'>백중세</span>"
        elif diff > 0:
            diff_html = f"<span class='tb-vs-diff blue'>블루 +{diff}</span>"
        else:
            diff_html = f"<span class='tb-vs-diff red'>레드 +{-diff}</span>"
        lane_uri = ctx["position_icon"](lane)
        lane_img = f"<img src='{lane_uri}'>" if lane_uri else ""
        rows.append(
            f"<div class='tb-vs-row'>"
            f"<div class='tb-vs-side'><div><div class='tb-vs-name'>{p1['name']}</div>"
            f"<div class='tb-vs-sub'>MMR {p1['mmr']} · AI {p1['avg_ai']:.1f}</div></div></div>"
            f"<div class='tb-vs-mid'>{lane_img}<div class='l'>{pos_kr.get(lane, lane)}</div>{diff_html}</div>"
            f"<div class='tb-vs-side right'><div><div class='tb-vs-name'>{p2['name']}</div>"
            f"<div class='tb-vs-sub'>MMR {p2['mmr']} · AI {p2['avg_ai']:.1f}</div></div></div>"
            f"</div>"
        )
    st.markdown("".join(rows), unsafe_allow_html=True)


def _render_plan(plan: dict, mode_key: str, profiles: dict, pair_stats: dict, ctx: dict):
    p_blue = plan["p_blue"] * 100

    # 요약 칩 바
    st.markdown(
        f"<div class='tb-chip-bar'>"
        f"<span class='tb-chip gold'>종합 밸런스 <b>{plan['overall']:.0f}점</b></span>"
        f"<span class='tb-chip'>MMR 차이 <b>{plan['mmr_diff']:.0f}점</b></span>"
        f"<span class='tb-chip'>예상 승률 <b>{p_blue:.0f}% : {100 - p_blue:.0f}%</b></span>"
        f"<span class='tb-chip'>라인 적합도 <b>{(plan['fit1'] + plan['fit2']) / 10 * 100:.0f}%</b></span>"
        f"<span class='tb-chip'>스타일 격차 <b>{plan['style_gap']:.2f}</b></span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # 밸런스 구성 점수 바
    score_rows = []
    mode_weights = MODES[mode_key]["score"]
    top_key = max(mode_weights, key=mode_weights.get)
    for key, label in SCORE_COMPONENT_LABELS.items():
        val = plan["scores"][key]
        top_cls = " top" if key == top_key else ""
        crown = "👑 " if key == top_key else ""
        score_rows.append(
            f"<div class='style-row'><div class='style-name'>{crown}{label}</div>"
            f"<div class='style-track'><div class='style-fill{top_cls}' style='width:{max(val, 3):.0f}%'></div></div>"
            f"<div class='style-score'>{val:.0f}</div></div>"
        )
    st.markdown(
        f"<div class='pattern-card'>{''.join(score_rows)}</div>",
        unsafe_allow_html=True,
    )
    st.caption("👑 = 선택한 밸런스 기준의 핵심 항목. 점수는 0~100 (높을수록 두 팀이 균형).")

    # 팀 패널
    col1, col2 = st.columns(2)
    for col, side, team_key, assign_key, name, mmr_key, prob in (
        (col1, "blue", "t1", "assign1", "🔵 블루팀 (1팀)", "mmr1", p_blue),
        (col2, "red", "t2", "assign2", "🔴 레드팀 (2팀)", "mmr2", 100 - p_blue),
    ):
        with col:
            st.markdown(
                f"<div class='tb-team-head {side}'>"
                f"<div><div class='t-name'>{name}</div>"
                f"<div class='t-sub'>평균 MMR {plan[mmr_key]:.0f} · 라인 적합도 {plan['fit1' if side == 'blue' else 'fit2']:.1f}/5</div></div>"
                f"<div class='t-prob'><div class='v'>{prob:.0f}%</div><div class='k'>예상 승률</div></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            assign = plan[assign_key]
            for lane in LANES:
                st.markdown(_player_card(profiles[assign[lane]], lane, side, ctx), unsafe_allow_html=True)

    # 라인별 맞대결
    st.markdown("<div class='section-label'>⚔️ 라인별 맞대결</div>", unsafe_allow_html=True)
    _render_lane_matchups(plan, profiles, ctx)

    # 팀 지표 비교 (레이더 + 표)
    st.markdown("<div class='section-label'>📊 팀 지표 비교</div>", unsafe_allow_html=True)
    agg1 = _team_aggregate(plan["t1"], profiles)
    agg2 = _team_aggregate(plan["t2"], profiles)
    rcol, tcol = st.columns([1.1, 1])
    with rcol:
        _render_radar(agg1, agg2, ctx)
    with tcol:
        fmt = {
            "MMR": "{:.0f}", "캐리력 (DPM)": "{:.0f}", "안정성 (KDA)": "{:.2f}",
            "한타 참여 (KP%)": "{:.1f}", "시야 (와드/판)": "{:.1f}",
            "최근 폼 (승률%)": "{:.0f}", "평균 AI점수": "{:.2f}",
        }
        table = pd.DataFrame([
            {
                "지표": k,
                "🔵 블루팀": fmt[k].format(agg1[k]),
                "🔴 레드팀": fmt[k].format(agg2[k]),
                "우세": "🔵" if agg1[k] > agg2[k] * 1.02 else ("🔴" if agg2[k] > agg1[k] * 1.02 else "≈"),
            }
            for k in agg1
        ])
        st.dataframe(table, width="stretch", hide_index=True, row_height=38)
        st.caption("레이더 축은 두 팀 중 높은 쪽을 100%로 정규화. '≈'는 2% 이내 차이.")

    # 선정 이유
    st.markdown("<div class='section-label'>🧠 이렇게 팀을 나눈 이유</div>", unsafe_allow_html=True)
    reasons = build_reasons(plan, mode_key, profiles, pair_stats)
    st.markdown(
        f"<div class='callout'><div class='ic'>🧩</div><div class='tx'>"
        f"<ul class='tb-reason'>{''.join(f'<li>{r}</li>' for r in reasons)}</ul>"
        f"</div></div>",
        unsafe_allow_html=True,
    )


def _render_selection(ctx: dict) -> list[int]:
    """체크박스 선택 UI. 선택된 player_id 목록을 반환한다."""
    dfp = ctx["df_players"]
    pos_kr = ctx["position_service"].POSITION_KR

    candidates = dfp.copy()
    candidates["nick"] = candidates["riot_accounts"].fillna("-").map(
        lambda s: str(s).split(",")[0].strip()
    )
    candidates = candidates.sort_values(
        "display_name", key=lambda s: s.map(lambda x: str(x).casefold())
    )

    top = st.columns([2.2, 1, 1])
    with top[0]:
        search = st.text_input(
            "🔍 플레이어 검색", "", placeholder="이름 또는 닉네임으로 검색",
            key="tb_search", label_visibility="collapsed",
        )
    with top[1]:
        if st.button("전체 해제", width="stretch"):
            for pid in candidates["id"]:
                st.session_state[f"tb_pick_{pid}"] = False
            st.rerun()
    with top[2]:
        sort_mmr = st.toggle("MMR순 정렬", key="tb_sort_mmr")
    if sort_mmr:
        candidates = candidates.sort_values("mmr", ascending=False)

    visible = candidates
    if search.strip():
        q = search.strip().casefold()
        visible = candidates[
            candidates["display_name"].map(lambda x: q in str(x).casefold())
            | candidates["nick"].map(lambda x: q in str(x).casefold())
        ]

    cols = st.columns(3)
    for i, (_, row) in enumerate(visible.iterrows()):
        pid = int(row["id"])
        nick = row["nick"]
        short_nick = nick if len(nick) <= 16 else nick[:15] + "…"
        with cols[i % 3]:
            st.checkbox(
                f"**{row['display_name']}** · {short_nick}",
                key=f"tb_pick_{pid}",
                help=f"{nick} · {row['current_tier_label']}",
            )
            st.caption(
                f"{pos_kr.get(row['main_pos'], '미정')}/{pos_kr.get(row['sub_pos'], '미정')}"
                f" · MMR {int(row['mmr'])} · {int(row['matches_played'])}판"
            )

    selected = [int(pid) for pid in candidates["id"] if st.session_state.get(f"tb_pick_{pid}")]

    # 선택 현황 바 — 인원수 + 주 포지션 커버리지.
    n = len(selected)
    cls = "full" if n == PICK_COUNT else ("over" if n > PICK_COUNT else "")
    sel_rows = candidates[candidates["id"].isin(selected)]
    pos_chips = []
    for lane in LANES:
        cnt = int((sel_rows["main_pos"] == lane).sum())
        missing = " missing" if (n and cnt == 0) else ""
        pos_chips.append(
            f"<span class='tb-pos-chip{missing}'>{pos_kr.get(lane, lane)} {cnt}</span>"
        )
    st.markdown(
        f"<div class='tb-count-bar'><span class='tb-count {cls}'>선택 {n} / {PICK_COUNT}명</span>"
        f"{''.join(pos_chips)}</div>",
        unsafe_allow_html=True,
    )
    if n and any((sel_rows["main_pos"] == lane).sum() == 0 for lane in LANES):
        st.caption("빨간 칩은 선택 인원 중 주 포지션이 없는 라인입니다 (부 포지션·자동 배정으로 채워집니다).")
    return selected


def render_team_builder(ctx: dict):
    st.markdown(_CSS, unsafe_allow_html=True)
    st.title("⚔️ 내전 팀 밸런스 시뮬레이터")
    st.caption(
        "10명을 선택하면 126개 조합을 전수 평가해 가장 균형 잡힌 팀을 구성합니다. "
        "밸런스 기준을 바꿔가며 1안/2안/3안을 비교해보세요."
    )

    dfp = ctx["df_players"]
    if len(dfp) < PICK_COUNT:
        st.warning(f"등록된 플레이어가 {len(dfp)}명입니다. 팀 구성에는 최소 {PICK_COUNT}명이 필요합니다.")
        return

    st.markdown("<div class='section-label'>1️⃣ 참가자 선택</div>", unsafe_allow_html=True)
    selected = _render_selection(ctx)

    st.markdown("<div class='section-label'>2️⃣ 밸런스 기준 선택</div>", unsafe_allow_html=True)
    mode_keys = list(MODES.keys())
    mode_key = st.radio(
        "밸런스 기준",
        mode_keys,
        format_func=lambda k: MODES[k]["label"],
        horizontal=True,
        key="tb_mode_radio",
        label_visibility="collapsed",
    )
    st.caption(MODES[mode_key]["desc"])

    can_run = len(selected) == PICK_COUNT
    if not can_run:
        st.info(f"플레이어를 정확히 {PICK_COUNT}명 선택하면 팀을 구성할 수 있습니다. (현재 {len(selected)}명)")
    if st.button("⚔️ 팀 구성하기", type="primary", disabled=not can_run, width="stretch"):
        with st.spinner("126개 조합을 평가하는 중..."):
            result = generate_plans(sorted(selected), mode_key, ctx)
        st.session_state["tb_result"] = {
            "mode_key": mode_key,
            "pids": sorted(selected),
            **result,
        }

    saved = st.session_state.get("tb_result")
    if not saved:
        return

    st.divider()
    st.markdown("<div class='section-label'>3️⃣ 팀 구성 결과</div>", unsafe_allow_html=True)

    if saved["pids"] != sorted(selected) or saved["mode_key"] != mode_key:
        st.warning("선택 인원 또는 밸런스 기준이 변경되었습니다. **팀 구성하기**를 다시 누르면 아래 결과가 갱신됩니다.")

    plans = saved["plans"]
    profiles = saved["profiles"]
    pair_stats = saved["pair_stats"]
    st.caption(f"기준: {MODES[saved['mode_key']]['label']} — 상위 {len(plans)}개 조합을 제시합니다.")

    # 1안/2안/3안 비교 요약
    compare_rows = []
    for i, plan in enumerate(plans):
        compare_rows.append({
            "안": f"{i + 1}안" + (" ⭐" if i == 0 else ""),
            "종합 점수": f"{plan['overall']:.0f}",
            "MMR 차이": f"{plan['mmr_diff']:.0f}점",
            "예상 승률(블루)": f"{plan['p_blue'] * 100:.0f}%",
            "라인 적합도": f"{(plan['fit1'] + plan['fit2']) / 10 * 100:.0f}%",
            "시너지 균형": f"{plan['scores']['synergy']:.0f}",
            "스타일 균형": f"{plan['scores']['style']:.0f}",
            "폼 균형": f"{plan['scores']['form']:.0f}",
        })
    st.dataframe(pd.DataFrame(compare_rows), width="stretch", hide_index=True, row_height=36)

    medals = ["🥇", "🥈", "🥉"]
    tabs = st.tabs([
        f"{medals[i]} {i + 1}안{' · 추천' if i == 0 else ''} ({plan['overall']:.0f}점)"
        for i, plan in enumerate(plans)
    ])
    for tab, plan in zip(tabs, plans):
        with tab:
            _render_plan(plan, saved["mode_key"], profiles, pair_stats, ctx)
