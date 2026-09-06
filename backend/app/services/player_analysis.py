"""
League of Legends 플레이어 경기 데이터 분석 엔진.

입력된 경기(participant) 데이터를 기반으로 강점/약점, 승패 패턴,
플레이 스타일, 챔피언 숙련도를 분석한다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Literal

import pandas as pd

Rating = Literal["높음", "보통", "낮음"]

METRIC_DEFS: dict[str, dict[str, Any]] = {
    "win_rate": {"label": "승률", "higher_better": True, "fmt": "{:.1f}%", "unit": "%", "desc": ""},
    "kda": {"label": "KDA", "higher_better": True, "fmt": "{:.2f}", "unit": "", "desc": "킬+어시 / 데스"},
    "kill_participation": {"label": "킬관여율", "higher_better": True, "fmt": "{:.1f}%", "unit": "%", "desc": "팀 킬 관여 비율"},
    "cspm": {"label": "CSPM", "higher_better": True, "fmt": "{:.1f}", "unit": "", "desc": "분당 CS"},
    "gpm": {"label": "GPM", "higher_better": True, "fmt": "{:.0f}", "unit": "", "desc": "분당 골드"},
    "dpm": {"label": "DPM", "higher_better": True, "fmt": "{:.0f}", "unit": "", "desc": "분당 피해량"},
    "damage_tank_ratio": {"label": "딜탱비", "higher_better": True, "fmt": "{:.2f}", "unit": "", "desc": "넣은 피해 / 받은 피해"},
    "deaths": {"label": "평균 데스", "higher_better": False, "fmt": "{:.1f}", "unit": "", "desc": "낮을수록 좋음"},
    "wards_placed": {"label": "와드 설치", "higher_better": True, "fmt": "{:.1f}", "unit": "", "desc": "경기당 와드 수"},
    "vision_wards_bought": {"label": "핑와 구매", "higher_better": True, "fmt": "{:.1f}", "unit": "", "desc": "경기당 제어와드"},
    "multikill_score": {"label": "멀티킬 점수", "higher_better": True, "fmt": "{:.2f}", "unit": "", "desc": "더블킬 이상 가중치"},
    "kills": {"label": "킬", "higher_better": True, "fmt": "{:.1f}", "unit": "", "desc": "경기당 킬"},
    "pings": {"label": "핑 사용", "higher_better": True, "fmt": "{:.1f}", "unit": "", "desc": "경기당 핑 횟수"},
}


def _metric_desc(metric_key: str) -> str:
    return METRIC_DEFS.get(metric_key, {}).get("desc", "") or ""

WIN_LOSS_METRICS = [
    "kda", "kill_participation", "cspm", "gpm", "dpm", "deaths", "damage_tank_ratio",
    "pings",
]

SUMMARY_METRICS = [
    "win_rate", "kda", "cspm", "gpm", "dpm", "kill_participation",
    "damage_tank_ratio", "deaths", "pings",
]

HIGH_THRESHOLD = 1.12
LOW_THRESHOLD = 0.88
MIN_CHAMP_GAMES = 2
# 포지션별 지표를 공개하기 위한 최소 경기 수. 이 값을 못 넘긴 포지션은 표본이
# 부족한 것으로 보고 잠금(locked) 처리한다.
MIN_POSITION_GAMES = 3

DAMAGE_DEALT_KEYS = ("total_damage_dealt", "totalDamageDealt")


def _parse_damage_dealt(raw_data: Any) -> float:
    parsed = _parse_raw_stat(raw_data, *DAMAGE_DEALT_KEYS, default=None)
    return float(parsed or 0.0)


def _parse_raw_stat(raw_data: Any, *keys: str, default: float | None = None) -> float | None:
    if not raw_data:
        return default
    try:
        payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        final_stats = payload.get("final_stat_dict", {})
        for source in (final_stats, payload):
            for key in keys:
                if key in source and source[key] is not None:
                    return float(source[key])
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return default


def _safe_div(numerator: float, denominator: float, fallback: float = 0.0) -> float:
    if denominator == 0:
        return fallback
    return numerator / denominator


_RESOLVE_CHAMPION_NAME = None


def _get_resolve_champion_name():
    global _RESOLVE_CHAMPION_NAME
    if _RESOLVE_CHAMPION_NAME is not None:
        return _RESOLVE_CHAMPION_NAME

    import importlib.util
    import os

    module_path = os.path.join(os.path.dirname(__file__), "champion_data.py")
    spec = importlib.util.spec_from_file_location("champion_data", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _RESOLVE_CHAMPION_NAME = module.resolve_champion_name
    return _RESOLVE_CHAMPION_NAME


def _format_champion_label(champion_id: int, champion_name: str | None = None) -> str:
    resolve_champion_name = _get_resolve_champion_name()
    return resolve_champion_name(int(champion_id), champion_name)


_POSITION_SERVICE = None


def _get_position_service():
    """position_service 모듈을 파일 경로로 지연 로드한다.

    이 파일은 대시보드에서 패키지가 아닌 단일 파일로 로드되기도 하므로
    `from app.services...` 대신 champion_data 와 동일한 방식으로 불러온다.
    """
    global _POSITION_SERVICE
    if _POSITION_SERVICE is not None:
        return _POSITION_SERVICE

    import importlib.util
    import os

    module_path = os.path.join(os.path.dirname(__file__), "position_service.py")
    spec = importlib.util.spec_from_file_location("position_service", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _POSITION_SERVICE = module
    return _POSITION_SERVICE


def _resolve_main_sub_position(player_df: pd.DataFrame) -> tuple[str, str]:
    """플레이어 경기에서 최신 가중 (주포지션, 부포지션)을 산정한다.

    game_creation 이 있으면 최신 경기일수록 큰 가중치를 줘서, 판수가 쌓이며
    주 포지션이 바뀌면 실시간으로 반영된다."""
    pos_service = _get_position_service()
    ordered = player_df
    if "game_creation" in player_df.columns:
        ordered = player_df.sort_values("game_creation", ascending=False)
    positions_newest_first = ordered["position"].tolist() if "position" in ordered.columns else []
    return pos_service.main_and_sub_position(positions_newest_first)


def enrich_participant_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """경기 단위 파생 지표를 계산한다."""
    resolve_champion_name = _get_resolve_champion_name()
    enriched = df.copy()
    if "champion_id" in enriched.columns:
        enriched["champion_name"] = enriched.apply(
            lambda row: resolve_champion_name(
                int(row.get("champion_id") or 0),
                row.get("champion_name"),
            ),
            axis=1,
        )
    enriched["game_duration"] = pd.to_numeric(enriched.get("game_duration"), errors="coerce").fillna(0)
    minutes = enriched["game_duration"].replace(0, pd.NA) / 60.0

    if "raw_data" in enriched.columns:
        enriched["kill_participation"] = enriched["raw_data"].apply(
            lambda raw: _parse_raw_stat(raw, "kill_point", "killPoint", "kill_participation", default=None)
        )
        enriched["cspm_from_raw"] = enriched["raw_data"].apply(
            lambda raw: _parse_raw_stat(raw, "cspm", default=None)
        )
        enriched["vision_wards_bought"] = enriched.apply(
            lambda row: _parse_raw_stat(
                row.get("raw_data"),
                "vision_wards_bought",
                "visionWardsBought",
                default=None,
            )
            if pd.isna(row.get("control_wards_bought")) or float(row.get("control_wards_bought") or 0) == 0
            else float(row.get("control_wards_bought") or 0),
            axis=1,
        )
        enriched["pings"] = enriched["raw_data"].apply(_parse_total_pings)
    else:
        enriched["kill_participation"] = None
        enriched["cspm_from_raw"] = None
        enriched["vision_wards_bought"] = pd.to_numeric(
            enriched.get("control_wards_bought"), errors="coerce"
        ).fillna(0)
        enriched["pings"] = 0.0

    enriched["total_damage_dealt"] = pd.to_numeric(
        enriched.get("total_damage_dealt"), errors="coerce"
    ).fillna(0.0)
    if "raw_data" in enriched.columns:
        missing_damage = enriched["total_damage_dealt"] == 0
        enriched.loc[missing_damage, "total_damage_dealt"] = enriched.loc[missing_damage, "raw_data"].apply(
            _parse_damage_dealt
        ).astype(float)
    enriched["total_damage_taken"] = pd.to_numeric(
        enriched.get("total_damage_taken"), errors="coerce"
    ).fillna(0).replace(0, pd.NA)
    enriched["gold_earned"] = pd.to_numeric(enriched.get("gold_earned"), errors="coerce").fillna(0)
    enriched["cs"] = pd.to_numeric(enriched.get("cs"), errors="coerce").fillna(0)
    enriched["kda"] = pd.to_numeric(enriched.get("kda"), errors="coerce").fillna(0)
    enriched["deaths"] = pd.to_numeric(enriched.get("deaths"), errors="coerce").fillna(0)
    enriched["kills"] = pd.to_numeric(enriched.get("kills"), errors="coerce").fillna(0)
    enriched["wards_placed"] = pd.to_numeric(enriched.get("wards_placed"), errors="coerce").fillna(0)

    for col in ("double_kills", "triple_kills", "quadra_kills", "penta_kills"):
        enriched[col] = pd.to_numeric(enriched.get(col), errors="coerce").fillna(0)

    enriched["multikill_score"] = (
        enriched["double_kills"] * 1
        + enriched["triple_kills"] * 2
        + enriched["quadra_kills"] * 3
        + enriched["penta_kills"] * 4
    )

    enriched["gpm"] = enriched["gold_earned"] / minutes
    enriched["dpm"] = enriched["total_damage_dealt"] / minutes
    enriched["cspm"] = enriched["cspm_from_raw"]
    fallback_cspm = enriched["cs"] / minutes
    enriched["cspm"] = enriched["cspm"].fillna(fallback_cspm)
    enriched["damage_tank_ratio"] = enriched["total_damage_dealt"] / enriched["total_damage_taken"]
    enriched["damage_tank_ratio"] = enriched["damage_tank_ratio"].fillna(0)

    enriched["vision_wards_bought"] = pd.to_numeric(enriched["vision_wards_bought"], errors="coerce").fillna(
        pd.to_numeric(enriched.get("control_wards_bought"), errors="coerce").fillna(0)
    )

    numeric_cols = [
        "kill_participation", "cspm", "gpm", "dpm", "damage_tank_ratio",
        "multikill_score", "vision_wards_bought", "pings",
    ]
    for col in numeric_cols:
        enriched[col] = pd.to_numeric(enriched[col], errors="coerce").fillna(0)

    enriched["win"] = enriched["win"].astype(bool)
    return enriched


def _mean(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return 0.0
    return float(values.mean())


def _compute_group_means(df: pd.DataFrame, group_col: str, metrics: list[str]) -> dict[Any, dict[str, float]]:
    if df.empty or group_col not in df.columns:
        return {}
    grouped: dict[Any, dict[str, float]] = {}
    for key, group in df.groupby(group_col):
        grouped[key] = {metric: _mean(group[metric]) for metric in metrics if metric in group.columns}
    return grouped


def _ratio(value: float, baseline: float, higher_better: bool) -> float:
    if baseline == 0:
        return 1.0 if value == 0 else (1.15 if higher_better else 0.85)
    if higher_better:
        return value / baseline
    return baseline / value if value > 0 else 1.15


def _classify_ratio(ratio: float) -> Rating:
    if ratio >= HIGH_THRESHOLD:
        return "높음"
    if ratio <= LOW_THRESHOLD:
        return "낮음"
    return "보통"


def _magnitude_rating(value: float, baseline: float) -> Rating:
    """지표의 '실제 수치 크기'를 기준 대비로 분류한다.

    품질 등급(_classify_ratio)과 달리 항상 값이 클수록 '높음'을 반환한다.
    예) 평균 데스가 평균보다 많으면 '높음' (품질로는 약점이지만 수치는 높음)."""
    return _classify_ratio(_ratio(value, baseline, higher_better=True))


def _format_metric(metric_key: str, value: float) -> str:
    fmt = METRIC_DEFS[metric_key]["fmt"]
    if metric_key == "win_rate":
        return fmt.format(value)
    return fmt.format(value)


def _relative_evaluation(
    metric_key: str,
    value: float,
    player_avg: float,
    champ_avg: float | None,
    position_avg: float | None,
) -> dict[str, Any]:
    higher_better = METRIC_DEFS[metric_key]["higher_better"]
    parts: list[str] = []

    # 표시용 등급은 '실제 수치 크기'(값이 클수록 높음)를 기준으로 한다.
    # 강점/약점 순위에 쓰는 품질 ratio와 분리해, 데스처럼 낮을수록 좋은
    # 지표에서 "데스 낮음"처럼 수치와 반대로 표기되는 문제를 막는다.
    parts.append(f"포지션 평균 대비 {_magnitude_rating(value, player_avg)}")

    if champ_avg is not None and champ_avg > 0:
        parts.append(f"챔피언 평균 대비 {_magnitude_rating(value, champ_avg)}")

    # 강점/약점 판정에 쓰는 품질 등급(높을수록 좋음)은 별도 보관.
    quality_ratio = _ratio(value, player_avg, higher_better)

    return {
        "rating": _classify_ratio(quality_ratio),
        # 실제 수치 크기 등급(값이 클수록 '높음'). 칩 텍스트 표기용.
        "magnitude_rating": _magnitude_rating(value, player_avg),
        "description": " · ".join(parts),
        "score": quality_ratio,
    }


def _position_mixture_baseline(
    all_df: pd.DataFrame,
    player_df: pd.DataFrame,
    metrics: list[str],
) -> dict[str, float]:
    """플레이어의 포지션 구성비로 가중한 비교 기준(혼합 평균)을 만든다.

    원딜 8판 + 서폿 8판을 소화한 플레이어라면 기준도
    (원딜 평균×8 + 서폿 평균×8) / 16 으로 섞는다. 혼합 플레이어의 전체 경기
    평균(CS 등이 중간값)이 주 포지션 하나의 평균과 비교되어 왜곡되는 문제를
    막는다. 단일 포지션 플레이어는 해당 포지션 평균과 동일하다.
    UNKNOWN 포지션 경기는 전체 평균을 기준으로 삼는다.
    """
    if player_df.empty or "position" not in player_df.columns:
        base = {m: _mean(all_df[m]) for m in metrics if m in all_df.columns}
        base["win_rate"] = float(all_df["win"].mean() * 100) if not all_df.empty else 0.0
        return base

    counts = player_df.groupby("position").size()
    total = int(counts.sum())
    acc: dict[str, float] = {m: 0.0 for m in metrics}
    win_acc = 0.0
    for pos, n in counts.items():
        pos_df = all_df[all_df["position"] == pos] if pos != "UNKNOWN" else all_df
        if pos_df.empty:
            pos_df = all_df
        for m in metrics:
            if m in pos_df.columns:
                acc[m] += _mean(pos_df[m]) * int(n)
        win_acc += float(pos_df["win"].mean() * 100) * int(n)
    baseline = {m: v / total for m, v in acc.items()}
    baseline["win_rate"] = win_acc / total
    return baseline


def _player_aggregate(player_df: pd.DataFrame) -> dict[str, float]:
    if player_df.empty:
        return {key: 0.0 for key in METRIC_DEFS}
    return {
        "win_rate": float(player_df["win"].mean() * 100),
        "kda": _mean(player_df["kda"]),
        "kill_participation": _mean(player_df["kill_participation"]),
        "cspm": _mean(player_df["cspm"]),
        "gpm": _mean(player_df["gpm"]),
        "dpm": _mean(player_df["dpm"]),
        "damage_tank_ratio": _mean(player_df["damage_tank_ratio"]),
        "deaths": _mean(player_df["deaths"]),
        "wards_placed": _mean(player_df["wards_placed"]),
        "vision_wards_bought": _mean(player_df["vision_wards_bought"]),
        "multikill_score": _mean(player_df["multikill_score"]),
        "kills": _mean(player_df["kills"]),
        "pings": _mean(player_df["pings"]) if "pings" in player_df.columns else 0.0,
        "matches": float(len(player_df)),
    }


def _champion_analysis(player_df: pd.DataFrame) -> dict[str, Any]:
    if player_df.empty:
        return {"best": None, "worst": None, "table": []}

    rows = []
    for champion_id, group in player_df.groupby("champion_id"):
        games = len(group)
        if games == 0:
            continue
        champ_name = _format_champion_label(
            int(champion_id),
            group.iloc[0].get("champion_name"),
        )
        win_rate = float(group["win"].mean() * 100)
        rows.append({
            "champion_id": int(champion_id),
            "champion_name": champ_name,
            "games": games,
            "win_rate": win_rate,
            "kda": _mean(group["kda"]),
            "cspm": _mean(group["cspm"]),
            "dpm": _mean(group["dpm"]),
            "gpm": _mean(group["gpm"]),
            "score": win_rate * 0.4 + _mean(group["kda"]) * 15 + _mean(group["dpm"]) / 1000,
        })

    qualified = [row for row in rows if row["games"] >= MIN_CHAMP_GAMES]
    if not qualified:
        qualified = rows

    if not qualified:
        return {"best": None, "worst": None, "table": rows}

    best = max(qualified, key=lambda row: (row["win_rate"], row["kda"], row["games"]))
    worst = min(qualified, key=lambda row: (row["win_rate"], row["kda"], -row["games"]))
    # 후보가 하나뿐이면 max·min이 같은 챔을 가리켜 최고=최악이 된다. 의미가 없으므로
    # 부진 챔은 표시하지 않는다(대시보드·봇 모두 worst=None 이면 해당 줄을 생략).
    if len(qualified) < 2 or worst["champion_id"] == best["champion_id"]:
        worst = None
    # 표에는 판수 조건(MIN_CHAMP_GAMES)과 무관하게 플레이한 모든 챔피언을 노출한다.
    # best/worst 하이라이트만 qualified(2판 이상) 중에서 고른다.
    table = sorted(rows, key=lambda row: (row["games"], row["win_rate"], row["kda"]), reverse=True)
    return {"best": best, "worst": worst, "table": table}


def _win_loss_analysis(player_df: pd.DataFrame) -> dict[str, Any]:
    wins = player_df[player_df["win"]]
    losses = player_df[~player_df["win"]]

    if wins.empty or losses.empty:
        return {
            "comparisons": [],
            "win_patterns": [],
            "loss_patterns": [],
            "win_condition": None,
            "insufficient_sample": True,
        }

    comparisons = []
    for metric in WIN_LOSS_METRICS:
        win_val = _mean(wins[metric])
        loss_val = _mean(losses[metric])
        higher_better = METRIC_DEFS[metric]["higher_better"]
        diff = win_val - loss_val
        impact = diff if higher_better else -diff
        sign = "+" if diff >= 0 else "-"
        comparisons.append({
            "metric": metric,
            "label": METRIC_DEFS[metric]["label"],
            "win_value": win_val,
            "loss_value": loss_val,
            "diff": diff,
            "impact": impact,
            "higher_better": higher_better,
            # 소비처(봇/대시보드)가 그대로 출력할 수 있는 포맷 문자열.
            "win_text": _format_metric(metric, win_val),
            "loss_text": _format_metric(metric, loss_val),
            "diff_text": f"{sign}{_format_metric(metric, abs(diff))}",
        })

    comparisons.sort(key=lambda item: item["impact"], reverse=True)
    return {
        "comparisons": comparisons,
        "win_patterns": comparisons[:3],
        "loss_patterns": sorted(comparisons, key=lambda item: item["impact"])[:3],
        "win_condition": comparisons[0] if comparisons else None,
        "insufficient_sample": False,
    }


def _score_style(
    player_stats: dict[str, float],
    position_stats: dict[str, float],
    weights: dict[str, float],
    *,
    invert: set[str] | None = None,
) -> float:
    invert = invert or set()
    scores = []
    for metric, weight in weights.items():
        player_val = player_stats.get(metric, 0.0)
        pos_val = position_stats.get(metric, 0.0)
        if metric in invert:
            ratio = _ratio(player_val, pos_val, higher_better=False)
        else:
            ratio = _ratio(player_val, pos_val, higher_better=True)
        scores.append(ratio * weight)
    if not scores:
        return 0.0
    return sum(scores) / sum(weights.values())


def _play_style_classification(
    player_stats: dict[str, float],
    position_stats: dict[str, float],
) -> dict[str, Any]:
    # (스타일명, 가중치, invert 집합) — invert는 '낮을수록 좋은' 지표(deaths 등).
    style_defs: list[tuple[str, dict[str, float], set[str]]] = [
        ("하드캐리형", {"dpm": 0.4, "multikill_score": 0.3, "kills": 0.3}, set()),
        ("폭딜누커형", {"dpm": 0.55, "damage_tank_ratio": 0.45}, set()),
        ("운영파밍형", {"cspm": 0.45, "gpm": 0.45, "deaths": 0.1}, {"deaths"}),
        ("한타지향형", {"kill_participation": 0.5, "dpm": 0.3, "multikill_score": 0.2}, set()),
        ("이니시에이터형", {"kill_participation": 0.5, "kills": 0.25, "damage_tank_ratio": 0.25}, {"damage_tank_ratio"}),
        ("시야장악형", {"wards_placed": 0.4, "vision_wards_bought": 0.35, "kill_participation": 0.25}, set()),
        ("철벽안정형", {"deaths": 0.5, "kda": 0.5}, {"deaths"}),
        ("멀티킬하이라이트형", {"multikill_score": 0.6, "kills": 0.4}, set()),
    ]
    styles = {
        name: _score_style(player_stats, position_stats, weights, invert=invert)
        for name, weights, invert in style_defs
    }

    ranked = sorted(styles.items(), key=lambda item: item[1], reverse=True)
    primary = ranked[0][0]
    secondary = ranked[1][0] if ranked[1][1] >= HIGH_THRESHOLD else None

    player_types = [primary]
    if secondary:
        player_types.append(secondary)

    return {
        "scores": styles,
        "recommended_style": primary,
        "player_types": player_types,
        "player_type_label": " + ".join(player_types),
    }


def _build_pattern_lines(comparisons: list[dict[str, Any]], *, positive: bool) -> list[str]:
    lines = []
    for item in comparisons:
        if positive and item["impact"] <= 0:
            continue
        if not positive and item["impact"] >= 0:
            continue
        label = item["label"]
        particle = _korean_subject_particle(label)
        win_text = _format_metric(item["metric"], item["win_value"])
        loss_text = _format_metric(item["metric"], item["loss_value"])
        # 데스처럼 '낮을수록 좋은' 지표는 방향 문구를 뒤집는다.
        direction = "높을수록" if item["higher_better"] else "낮을수록"
        if positive:
            lines.append(
                f"승리 시 {label} {win_text}, 패배 시 {loss_text} → "
                f"{label}{particle} {direction} 승리 확률 상승"
            )
        else:
            opposite = "높음" if item["higher_better"] else "낮음"
            lines.append(
                f"승리 시 {label} {win_text}, 패배 시 {loss_text} → "
                f"패배 경기에서 오히려 {label}{particle} {opposite} (승리로 연결되지 않는 지표)"
            )
    return lines[:3]


def _build_win_condition_text(win_condition: dict[str, Any] | None, *, insufficient_sample: bool = False) -> str:
    if insufficient_sample:
        return "승리/패배 경기가 모두 필요합니다. 더 많은 경기 데이터가 쌓이면 핵심 승리 조건을 도출할 수 있습니다."
    if not win_condition:
        return "표본이 부족하여 핵심 승리 조건을 도출하기 어렵습니다."
    label = win_condition["label"]
    win_text = _format_metric(win_condition["metric"], win_condition["win_value"])
    loss_text = _format_metric(win_condition["metric"], win_condition["loss_value"])
    if win_condition["higher_better"]:
        return (
            f"{label} 격차가 가장 큽니다 (승리 {win_text} vs 패배 {loss_text}). "
            f"{label} 확보가 이 플레이어의 핵심 승리 조건입니다."
        )
    return (
        f"{label} 격차가 가장 큽니다 (승리 {win_text} vs 패배 {loss_text}). "
        f"데스를 줄이는 안정적인 플레이가 승리의 핵심입니다."
    )


def _korean_subject_particle(word: str) -> str:
    """받침 유무에 따라 '이' 또는 '가'를 반환한다."""
    if not word:
        return "이"
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return "이" if (code - 0xAC00) % 28 != 0 else "가"
    return "이"


def _build_one_liner(
    player_stats: dict[str, float],
    style: dict[str, Any],
    win_condition: dict[str, Any] | None,
    strengths: list[dict[str, Any]],
) -> str:
    type_label = style["player_type_label"]
    win_rate = player_stats["win_rate"]
    top_strength = strengths[0]["label"] if strengths else "전투 기여"
    if win_condition:
        wc_label = win_condition["label"]
        wc_win = _format_metric(win_condition["metric"], win_condition["win_value"])
        return (
            f"{type_label} 성향의 플레이어로, 승리 시 {wc_label} {wc_win} 수준을 유지할 때 "
            f"승률 {win_rate:.1f}%를 끌어올립니다. {top_strength}에서 두드러지며, "
            f"자신의 강점을 살린 {style['recommended_style']} 플레이가 최적입니다."
        )
    particle = _korean_subject_particle(top_strength)
    return (
        f"{type_label} 성향의 플레이어로 {top_strength}{particle} 강점입니다. "
        f"현재 승률 {win_rate:.1f}%이며, 데이터가 더 쌓이면 승리 공식이 선명해집니다."
    )


def _rank_strengths_weaknesses(
    player_stats: dict[str, float],
    position_stats: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scored = []
    for metric, meta in METRIC_DEFS.items():
        if metric in ("win_rate", "matches"):
            continue
        player_val = player_stats.get(metric, 0.0)
        pos_val = position_stats.get(metric, 0.0)
        ratio = _ratio(player_val, pos_val, meta["higher_better"])
        scored.append({
            "metric": metric,
            "label": meta["label"],
            "value": player_val,
            "position_avg": pos_val,
            "ratio": ratio,
            # rating: 품질 등급(순위 정렬용). display_rating: 실제 수치 크기(표기용).
            "rating": _classify_ratio(ratio),
            "display_rating": _magnitude_rating(player_val, pos_val),
        })

    strengths = sorted(scored, key=lambda item: item["ratio"], reverse=True)[:3]
    weaknesses = sorted(scored, key=lambda item: item["ratio"])[:3]
    return strengths, weaknesses


def _load_raw(raw: Any) -> dict | None:
    """raw_data(JSON 문자열/딕트)를 안전하게 dict로 로드한다."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ── A1. 핑 성향 ─────────────────────────────────────────────────────────
# 감사 결과 전 경기에서 항상 0이라 신호가 없는 핑(bait/danger/hold/vision_cleared)은 제외.
LIVE_PING_KEYS = [
    "on_my_way_ping", "assists_me_ping",
    "enemy_missing_ping", "enemy_vision_ping", "need_vision_ping",
    "all_in_ping", "push_ping", "get_back_ping",
]
# 카테고리: (포함 핑 키, 이모지, 설명)
PING_CATEGORIES: dict[str, tuple[list[str], str, str]] = {
    "합류·로밍형": (["on_my_way_ping", "assists_me_ping"], "🏃", "한타 합류·도움 요청 콜이 많은 협력형"),
    "시야·정보형": (["enemy_missing_ping", "enemy_vision_ping", "need_vision_ping"], "👁️", "적 미아·시야 정보를 부지런히 공유하는 형"),
    "공격적 오더형": (["all_in_ping", "push_ping"], "🗡️", "들이대기·압박 콜로 교전을 주도하는 형"),
    "신중·운영형": (["get_back_ping"], "🛡️", "후퇴 콜로 위기를 관리하는 형"),
}


def _parse_ping_dict(raw: Any) -> dict[str, float]:
    payload = _load_raw(raw) or {}
    d = payload.get("total_pings_dict") or {}
    out: dict[str, float] = {}
    for key in LIVE_PING_KEYS + ["total_pings"]:
        try:
            out[key] = float(d.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def _parse_total_pings(raw: Any) -> float:
    """경기당 총 핑 수. total_pings 필드가 없거나 0이면 개별 핑 합으로 보정한다."""
    p = _parse_ping_dict(raw)
    return max(p["total_pings"], sum(p[k] for k in LIVE_PING_KEYS))


def _ping_profile(player_df: pd.DataFrame) -> dict[str, Any] | None:
    if "raw_data" not in player_df.columns or player_df.empty:
        return None
    pings = [_parse_ping_dict(r) for r in player_df["raw_data"]]
    games = len(pings)
    if games == 0:
        return None

    # total_pings 필드가 비어 있어도 개별 핑 합계로 보정해, 콜이 많은
    # 플레이어가 '과묵한 솔로형'으로 오분류되지 않게 한다.
    avg_total = sum(
        max(p["total_pings"], sum(p[k] for k in LIVE_PING_KEYS)) for p in pings
    ) / games
    cat_per_game = {
        name: sum(sum(p[k] for k in keys) for p in pings) / games
        for name, (keys, _e, _d) in PING_CATEGORIES.items()
    }
    live_sum = sum(cat_per_game.values())

    # 콜 자체가 거의 없으면 '과묵한 솔로형'.
    if avg_total < 3 or live_sum <= 0:
        return {
            "tag": "과묵한 솔로형", "emoji": "🤐",
            "description": "핑/콜이 적은 과묵한 플레이형",
            "avg_total": round(avg_total, 1),
            "dominant_share": 0,
            "breakdown": {k: round(v, 1) for k, v in cat_per_game.items()},
        }

    top = max(cat_per_game, key=cat_per_game.get)
    _keys, emoji, desc = PING_CATEGORIES[top]
    return {
        "tag": top, "emoji": emoji, "description": desc,
        "avg_total": round(avg_total, 1),
        "dominant_share": round(cat_per_game[top] / live_sum * 100),
        "breakdown": {k: round(v, 1) for k, v in cat_per_game.items()},
    }


# ── A2. 라인전 vs 한타 ───────────────────────────────────────────────────
def _lane_performance(player_df: pd.DataFrame) -> dict[str, Any] | None:
    if "raw_data" not in player_df.columns or player_df.empty:
        return None
    rows = []
    for raw in player_df["raw_data"]:
        payload = _load_raw(raw)
        if not payload:
            continue
        ls = payload.get("lane_stat_dict") or {}
        fs = payload.get("final_stat_dict") or {}
        if not ls:
            continue
        rows.append({
            "lane_ai": float(ls.get("ai_score") or 0),
            "lane_rank": float(ls.get("ai_score_rank") or 0),
            "final_ai": float(fs.get("ai_score") or 0),
            "final_rank": float(fs.get("ai_score_rank") or 0),
        })
    if not rows:
        return None

    n = len(rows)
    lane_ai = sum(r["lane_ai"] for r in rows) / n
    final_ai = sum(r["final_ai"] for r in rows) / n
    lane_rank = sum(r["lane_rank"] for r in rows if r["lane_rank"]) / max(1, sum(1 for r in rows if r["lane_rank"]))
    final_rank = sum(r["final_rank"] for r in rows if r["final_rank"]) / max(1, sum(1 for r in rows if r["final_rank"]))

    def _stars(rank: float) -> int:
        # ai_score_rank: 1(최고)~10(최저) → 별 5~1개.
        if rank <= 0:
            return 3
        return max(1, min(5, round((11 - rank) / 2)))

    # 순위가 라인전→최종에 좋아지면(숫자↓) 후반 캐리형.
    rank_delta = lane_rank - final_rank
    if rank_delta >= 1.0:
        growth_tag, growth_emoji = "후반 캐리형", "📈"
    elif rank_delta <= -1.0:
        growth_tag, growth_emoji = "초반 강세형", "⚡"
    else:
        growth_tag, growth_emoji = "꾸준형", "➡️"

    return {
        "lane_ai": round(lane_ai, 1),
        "final_ai": round(final_ai, 1),
        "lane_rank": round(lane_rank, 1),
        "final_rank": round(final_rank, 1),
        "lane_stars": _stars(lane_rank),
        "teamfight_stars": _stars(final_rank),
        "growth_tag": growth_tag,
        "growth_emoji": growth_emoji,
        "rank_delta": round(rank_delta, 1),
    }


# ── A2-2. 라인전 → 후반 정량 분석 (lane_stat_dict + final_stat_dict) ─────
# lane_stat_dict 는 라인전 종료 시점의 누적 스탯, final_stat_dict 는 풀게임 스탯.
# 두 시점을 비교해 우위 유지·후반 캐리·시야 증가·성장 속도를 정량화한다.
LANE_AHEAD_RANK = 4   # ai_score_rank 1~10 중 이 순위 이하면 '라인전 우위'로 본다.


def _extract_lane_final_rows(df: pd.DataFrame) -> list[dict[str, float]]:
    """각 경기의 lane/final 스탯 쌍을 뽑는다. 둘 중 하나라도 없으면 제외."""
    rows: list[dict[str, float]] = []
    if "raw_data" not in df.columns:
        return rows
    for raw in df["raw_data"]:
        payload = _load_raw(raw)
        if not payload:
            continue
        ls = payload.get("lane_stat_dict") or {}
        fs = payload.get("final_stat_dict") or {}
        if not ls or not fs:
            continue

        def _f(d: dict, key: str) -> float:
            try:
                return float(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        rows.append({
            "lane_rank": _f(ls, "ai_score_rank"), "final_rank": _f(fs, "ai_score_rank"),
            "lane_ai": _f(ls, "ai_score"), "final_ai": _f(fs, "ai_score"),
            "lane_kp": _f(ls, "kill_point"), "final_kp": _f(fs, "kill_point"),
            "lane_wards": _f(ls, "wards_placed"), "final_wards": _f(fs, "wards_placed"),
            "lane_pinks": _f(ls, "vision_wards_bought"), "final_pinks": _f(fs, "vision_wards_bought"),
            "lane_cspm": _f(ls, "cspm"), "lane_cs": _f(ls, "cs"),
            "lane_level": _f(ls, "champion_level"),
        })
    return rows


def _lane_final_analysis(
    player_df: pd.DataFrame,
    all_df: pd.DataFrame,
) -> dict[str, Any] | None:
    """lane/final 스탯을 비교한 4가지 정량 분석.

    비교 기준은 플레이어의 포지션 구성비로 가중한 혼합 평균이다
    (종합 지표의 _position_mixture_baseline 과 동일 원칙).
    """
    rows = _extract_lane_final_rows(player_df)
    if not rows:
        return None
    n = len(rows)

    def _avg(rs: list[dict[str, float]], key: str) -> float:
        vals = [r[key] for r in rs]
        return sum(vals) / len(vals) if vals else 0.0

    # 포지션별 기준 행을 뽑아 플레이어 판수 비율로 가중한다.
    pos_base_rows: dict[Any, list[dict[str, float]]] = {}
    pos_counts = None
    if "position" in player_df.columns and "position" in all_df.columns:
        counted = player_df[player_df["position"] != "UNKNOWN"]
        if not counted.empty:
            pos_counts = counted.groupby("position").size()
            for pos in pos_counts.index:
                extracted = _extract_lane_final_rows(all_df[all_df["position"] == pos])
                if extracted:
                    pos_base_rows[pos] = extracted

    if pos_counts is not None and pos_base_rows:
        weight_total = sum(int(pos_counts[pos]) for pos in pos_base_rows)

        def _base_avg(key: str) -> float:
            return sum(
                _avg(pos_base_rows[pos], key) * int(pos_counts[pos]) for pos in pos_base_rows
            ) / weight_total

        baseline_games = sum(len(rs) for rs in pos_base_rows.values())
    else:
        fallback_rows = _extract_lane_final_rows(all_df) or rows

        def _base_avg(key: str) -> float:
            return _avg(fallback_rows, key)

        baseline_games = len(fallback_rows)

    items: list[dict[str, Any]] = []

    # 1) 라인전 우위 유지율 — 라인전 상위권(1~4위) 경기를 최종에도 상위권으로 마친 비율.
    ahead = [r for r in rows if 0 < r["lane_rank"] <= LANE_AHEAD_RANK]
    retained = [r for r in ahead if 0 < r["final_rank"] <= LANE_AHEAD_RANK]
    retention = {
        "ahead_games": len(ahead),
        "retained_games": len(retained),
        "rate": round(len(retained) / len(ahead) * 100) if ahead else None,
    }
    if ahead:
        rate = retention["rate"]
        rating: Rating = "높음" if rate >= 70 else ("낮음" if rate <= 40 else "보통")
        items.append({
            "key": "advantage_retention", "emoji": "🏁", "label": "라인전 우위 유지",
            "value": f"{rate}%", "rating": rating,
            "detail": f"라인전 상위권(팀 내 {LANE_AHEAD_RANK}위 이내) {len(ahead)}판 중 {len(retained)}판을 끝까지 상위권으로 마무리",
        })
    else:
        items.append({
            "key": "advantage_retention", "emoji": "🏁", "label": "라인전 우위 유지",
            "value": "—", "rating": "보통",
            "detail": f"라인전 상위권({LANE_AHEAD_RANK}위 이내)으로 마친 경기가 아직 없습니다",
        })

    # 2) 후반 캐리력 — 라인전→최종 순위 상승폭 + 라인전 이후 킬관여 상승.
    rank_delta = _avg(rows, "lane_rank") - _avg(rows, "final_rank")  # +면 후반에 순위 상승
    kp_delta = _avg(rows, "final_kp") - _avg(rows, "lane_kp")
    base_kp_delta = _base_avg("final_kp") - _base_avg("lane_kp")
    improved = sum(1 for r in rows if r["final_rank"] and r["lane_rank"] and r["final_rank"] < r["lane_rank"])
    late_carry = {
        "rank_delta": round(rank_delta, 1),
        "kp_delta": round(kp_delta, 1),
        "baseline_kp_delta": round(base_kp_delta, 1),
        "improved_games": improved,
        "improved_rate": round(improved / n * 100),
    }
    if rank_delta >= 0.5:
        rating = "높음"
    elif rank_delta <= -0.5:
        rating = "낮음"
    else:
        rating = "보통"
    items.append({
        "key": "late_carry", "emoji": "🚀", "label": "후반 캐리력",
        "value": f"{rank_delta:+.1f}순위", "rating": rating,
        "detail": (
            f"라인전 이후 팀 내 순위 변화 평균 {rank_delta:+.1f} · "
            f"{n}판 중 {improved}판({late_carry['improved_rate']}%)에서 순위 상승 · "
            f"킬관여 {kp_delta:+.1f}%p (포지션 평균 {base_kp_delta:+.1f}%p)"
        ),
    })

    # 3) 시야 기여 증가 — 라인전 와드 대비 라인전 이후 추가 설치량.
    lane_wards = _avg(rows, "lane_wards")
    post_wards = _avg(rows, "final_wards") - lane_wards
    base_post_wards = _base_avg("final_wards") - _base_avg("lane_wards")
    pinks_added = _avg(rows, "final_pinks") - _avg(rows, "lane_pinks")
    growth_pct = round(post_wards / lane_wards * 100) if lane_wards > 0 else None
    vision_growth = {
        "lane_wards": round(lane_wards, 1),
        "post_wards": round(post_wards, 1),
        "baseline_post_wards": round(base_post_wards, 1),
        "pinks_added": round(pinks_added, 1),
        "growth_pct": growth_pct,
    }
    rating = _classify_ratio(_ratio(post_wards, base_post_wards, higher_better=True))
    value_text = f"+{growth_pct}%" if growth_pct is not None else f"+{post_wards:.1f}개"
    items.append({
        "key": "vision_growth", "emoji": "👁️", "label": "시야 기여 증가",
        "value": value_text, "rating": rating,
        "detail": (
            f"라인전 와드 {lane_wards:.1f}개 → 이후 +{post_wards:.1f}개 추가 설치 "
            f"(포지션 평균 +{base_post_wards:.1f}개) · 라인전 이후 제어와드 +{pinks_added:.1f}개"
        ),
    })

    # 4) 성장 속도 — 라인전 단계 분당 CS·레벨을 같은 포지션 평균과 비교.
    lane_cspm = _avg(rows, "lane_cspm")
    base_lane_cspm = _base_avg("lane_cspm")
    lane_level = _avg(rows, "lane_level")
    base_lane_level = _base_avg("lane_level")
    cspm_ratio = _ratio(lane_cspm, base_lane_cspm, higher_better=True)
    growth_speed = {
        "lane_cspm": round(lane_cspm, 1),
        "baseline_lane_cspm": round(base_lane_cspm, 1),
        "lane_level": round(lane_level, 1),
        "baseline_lane_level": round(base_lane_level, 1),
        "cspm_ratio": round(cspm_ratio, 2),
    }
    items.append({
        "key": "growth_speed", "emoji": "🌱", "label": "성장 속도",
        "value": f"CS/분 {lane_cspm:.1f}", "rating": _classify_ratio(cspm_ratio),
        "detail": (
            f"라인전 분당 CS {lane_cspm:.1f} (포지션 평균 {base_lane_cspm:.1f}, {cspm_ratio * 100:.0f}%) · "
            f"라인 종료 레벨 {lane_level:.1f} (평균 {base_lane_level:.1f})"
        ),
    })

    return {
        "games": n,
        "baseline_games": baseline_games,
        "items": items,
        "advantage_retention": retention,
        "late_carry": late_carry,
        "vision_growth": vision_growth,
        "growth_speed": growth_speed,
    }


# ── C. 최근 폼/추세 ──────────────────────────────────────────────────────
def _recent_form(player_df: pd.DataFrame) -> dict[str, Any] | None:
    if player_df.empty:
        return None
    df = player_df
    if "game_creation" in df.columns:
        df = df.sort_values("game_creation", ascending=False)
    results = [bool(w) for w in df["win"].tolist()]  # 최신순
    if not results:
        return None

    last5 = results[:5]
    n5 = len(last5)
    wins5 = sum(1 for w in last5 if w)

    streak_type = results[0]
    streak = 0
    for w in results:
        if w == streak_type:
            streak += 1
        else:
            break

    if streak >= 3 and streak_type:
        tag, emoji = f"{streak}연승 · 폼 상승중", "🔥"
    elif streak >= 3 and not streak_type:
        tag, emoji = f"{streak}연패 · 주의", "🧊"
    elif wins5 >= 4:
        tag, emoji = "상승세", "📈"
    elif n5 >= 3 and wins5 <= 1:
        tag, emoji = "하락세", "📉"
    else:
        tag, emoji = "기복형", "🎢"

    return {
        "last5": ["승" if w else "패" for w in last5],
        "streak": streak,
        "streak_type": "승" if streak_type else "패",
        "recent_win_rate": round(wins5 / n5 * 100) if n5 else 0,
        "tag": tag,
        "emoji": emoji,
    }


# ── B. 시너지/천적 ───────────────────────────────────────────────────────
def _shrink_win_rate(wins: float, games: float, prior: float = 0.5, k: float = 2.0) -> float:
    """소표본 과대평가를 막기 위해 승률을 0.5로 약하게 수축(베이지안)."""
    return (wins + prior * k) / (games + k)


def _synergy_analysis(
    all_df: pd.DataFrame,
    player_id: int,
    name_map: dict[int, str],
    *,
    min_games: int = 2,
) -> dict[str, Any] | None:
    if all_df.empty or "team_id" not in all_df.columns:
        return None
    df = all_df[all_df["player_id"].notna()]
    my = df[df["player_id"] == player_id]
    if my.empty:
        return None

    my_team = {row.match_id: row.team_id for row in my.itertuples()}
    my_win = {row.match_id: bool(row.win) for row in my.itertuples()}
    my_matches = set(my_team)

    teammate: dict[int, list[int]] = defaultdict(lambda: [0, 0])  # pid -> [games, wins]
    enemy: dict[int, list[int]] = defaultdict(lambda: [0, 0])     # pid -> [games, my_wins]
    for row in df.itertuples():
        if row.match_id not in my_matches:
            continue
        pid = row.player_id
        if pid is None or int(pid) == player_id:
            continue
        pid = int(pid)
        won = my_win[row.match_id]
        if row.team_id == my_team[row.match_id]:
            teammate[pid][0] += 1
            teammate[pid][1] += 1 if won else 0
        else:
            enemy[pid][0] += 1
            enemy[pid][1] += 1 if won else 0

    def _pick(bucket: dict[int, list[int]], *, best: bool) -> dict[str, Any] | None:
        cand = [(pid, g, w) for pid, (g, w) in bucket.items() if g >= min_games]
        if not cand:
            return None
        cand.sort(key=lambda x: _shrink_win_rate(x[2], x[1]), reverse=best)
        pid, g, w = cand[0]
        return {
            "player_id": pid,
            "name": name_map.get(pid, f"#{pid}"),
            "games": g,
            "wins": w,
            "losses": g - w,
            "win_rate": round(w / g * 100) if g else 0,
        }

    best_duo = _pick(teammate, best=True)
    worst_duo = _pick(teammate, best=False)
    nemesis = _pick(enemy, best=False)        # 상대일 때 내 승률 최저 = 천적
    favorite_prey = _pick(enemy, best=True)   # 상대일 때 내 승률 최고 = 밥줄

    # 의미 있는 신호만 노출한다. (전승/전패 플레이어가 모든 상대를 천적/밥줄로
    # 표시하는 등 오해를 막기 위해 승률 임계로 거른다.)
    if best_duo and best_duo["win_rate"] < 55:
        best_duo = None
    if worst_duo and worst_duo["win_rate"] > 45:
        worst_duo = None
    if worst_duo and best_duo and worst_duo["player_id"] == best_duo["player_id"]:
        worst_duo = None
    if nemesis and nemesis["win_rate"] > 45:
        nemesis = None
    if favorite_prey and favorite_prey["win_rate"] < 55:
        favorite_prey = None
    if favorite_prey and nemesis and favorite_prey["player_id"] == nemesis["player_id"]:
        favorite_prey = None

    if not any([best_duo, worst_duo, nemesis, favorite_prey]):
        return None
    return {
        "best_duo": best_duo,
        "worst_duo": worst_duo,
        "nemesis": nemesis,
        "favorite_prey": favorite_prey,
        "min_games": min_games,
    }


# ── A5. 모멘텀(역전/리드) — time_analysis 기반 ───────────────────────────
def _momentum_analysis(
    player_df: pd.DataFrame,
    match_timelines: dict[str, dict] | None,
) -> dict[str, Any] | None:
    if not match_timelines:
        return None
    comeback = collapse = lead_secured = counted = 0
    for row in player_df.itertuples():
        ta = match_timelines.get(row.match_id)
        if not ta:
            continue
        seq = ta.get("concat_seq_dict") or {}
        if not seq:
            continue
        key = "B_Avg" if row.team_id == 100 else "R_Avg"
        series = []
        for mk in sorted(seq, key=lambda x: int(x) if str(x).isdigit() else 0):
            v = seq[mk]
            if isinstance(v, dict) and key in v:
                try:
                    series.append(float(v[key]))
                except (TypeError, ValueError):
                    pass
        if len(series) < 3:
            continue
        counted += 1
        lo, hi, won = min(series), max(series), bool(row.win)
        # time_analysis의 승률값은 50 근처로 압축돼 있어(관측 38~68) 상대 임계를 쓴다.
        # 50 미만이면 열세, 초과면 우세로 본다.
        if won and lo <= 43:          # 한때 명확히 열세였는데 이김 → 역전승
            comeback += 1
        if (not won) and hi >= 57:    # 한때 명확히 우세였는데 짐 → 리드 놓침
            collapse += 1
        if won and lo >= 47:          # 거의 끌려가지 않고 이김 → 리드 유지
            lead_secured += 1

    if counted == 0:
        return None
    tags = []
    if comeback >= 2:
        tags.append({"emoji": "🔄", "label": "역전의 명수", "desc": f"열세를 뒤집은 승리 {comeback}회"})
    if collapse >= 2:
        tags.append({"emoji": "🧨", "label": "리드 관리 주의", "desc": f"우세를 놓친 패배 {collapse}회"})
    if lead_secured >= 3 and collapse == 0:
        tags.append({"emoji": "🛡️", "label": "리드 굳히기 장인", "desc": "앞선 경기를 확실히 마무리"})
    return {
        "games_tracked": counted,
        "comeback_wins": comeback,
        "collapse_losses": collapse,
        "lead_secured_wins": lead_secured,
        "tags": tags,
    }


def _build_player_name_map(all_df: pd.DataFrame) -> dict[int, str]:
    """player_id → 표시 이름. display_name(있으면) 우선, 없으면 summoner_name."""
    name_map: dict[int, str] = {}
    if "player_id" not in all_df.columns:
        return name_map
    has_display = "display_name" in all_df.columns
    for row in all_df.itertuples():
        pid = getattr(row, "player_id", None)
        if pid is None or (isinstance(pid, float) and pd.isna(pid)):
            continue
        pid = int(pid)
        if pid in name_map:
            continue
        name = None
        if has_display:
            name = getattr(row, "display_name", None)
        if not name or (isinstance(name, float) and pd.isna(name)):
            name = getattr(row, "summoner_name", None)
        if name and not (isinstance(name, float) and pd.isna(name)):
            name_map[pid] = str(name)
    return name_map


def _build_summary_rows(
    player_stats: dict[str, float],
    position_baseline: dict[str, float],
    champ_weighted: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """종합 지표 카드용 행 목록을 만든다.

    position_baseline 은 '비교 기준'이 되는 포지션 평균(win_rate 포함)이며,
    champ_weighted 가 주어지면 '챔피언 평균 대비' 비교도 함께 표기한다.
    포지션별 지표에서는 챔피언 비교가 무의미해 champ_weighted=None 으로 호출한다.
    """
    rows = []
    for metric in SUMMARY_METRICS:
        value = player_stats.get(metric, 0.0)
        position_avg = position_baseline.get(metric, 0.0)
        champ_avg = champ_weighted.get(metric) if champ_weighted else None
        rel = _relative_evaluation(metric, value, position_avg, champ_avg, None)
        rows.append({
            "metric": metric,
            "label": METRIC_DEFS[metric]["label"],
            "desc": _metric_desc(metric),
            "value": value,
            "formatted_value": _format_metric(metric, value),
            "relative_rating": rel["rating"],
            "magnitude_rating": rel["magnitude_rating"],
            "relative_evaluation": rel["description"],
            # 정량 표시용 — 비교 기준(포지션 평균)의 실제 수치와 대비 백분율.
            "position_avg": position_avg,
            "formatted_position_avg": _format_metric(metric, position_avg),
            "pct_vs_avg": round(value / position_avg * 100) if position_avg > 0 else None,
        })
    return rows


def _position_breakdown(
    all_df: pd.DataFrame,
    player_df: pd.DataFrame,
    *,
    min_position_games: int,
) -> list[dict[str, Any]]:
    """플레이어가 플레이한 포지션마다 지표를 따로 산출한다.

    각 포지션의 비교 기준은 '같은 포지션 전체 플레이어' 평균이다(통합 지표가
    원딜/서폿을 오가는 플레이어에서 왜곡되는 문제를 포지션별로 분리해 해소).
    표본(min_position_games)을 못 넘긴 포지션은 locked=True 로 잠금 처리하고,
    그 포지션의 지표는 계산하지 않는다(데이터 부족).
    경기가 많은 포지션부터, 동률이면 표준 포지션 순서대로 정렬한다.
    """
    pos_service = _get_position_service()
    if "position" not in player_df.columns or player_df.empty:
        return []
    played = player_df[player_df["position"] != "UNKNOWN"]
    if played.empty:
        return []

    order = {pos: i for i, pos in enumerate(pos_service.CANONICAL_POSITIONS)}
    counts = played.groupby("position").size()
    positions = sorted(
        counts.index,
        key=lambda pos: (-int(counts[pos]), order.get(pos, 99)),
    )

    breakdown: list[dict[str, Any]] = []
    for position in positions:
        games = int(counts[position])
        entry: dict[str, Any] = {
            "position": position,
            "position_label": pos_service.position_label(position),
            "games": games,
            "min_games": min_position_games,
            "locked": games < min_position_games,
            "summary_table": [],
        }
        if not entry["locked"]:
            pos_baseline = _player_aggregate(all_df[all_df["position"] == position])
            player_pos_stats = _player_aggregate(played[played["position"] == position])
            entry["summary_table"] = _build_summary_rows(player_pos_stats, pos_baseline)
        breakdown.append(entry)
    return breakdown


def analyze_player(
    player_id: int,
    df_participants: pd.DataFrame,
    *,
    min_games: int = 1,
    min_position_games: int = MIN_POSITION_GAMES,
    match_timelines: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """플레이어 분석 리포트를 생성한다."""
    all_df = enrich_participant_metrics(df_participants)
    player_df = all_df[all_df["player_id"] == player_id].copy()

    if player_df.empty:
        return {"error": "플레이어 경기 데이터가 없습니다.", "player_id": player_id}

    metrics_for_baseline = [
        key for key in METRIC_DEFS
        if key not in ("win_rate", "matches")
    ]

    # 주/부 포지션은 최신 가중으로 산정해, 판수가 쌓이며 주 라인이 바뀌면 즉시 반영한다.
    position, sub_position = _resolve_main_sub_position(player_df)
    # 비교 기준(baseline)은 플레이어의 포지션 구성비로 가중한 혼합 평균이다.
    # 단일 포지션이면 그 포지션 전체 플레이어 평균과 같고, 원딜/서폿을 오가는
    # 플레이어면 두 포지션 평균을 판수 비율로 섞어 공정하게 비교한다.
    # (win_rate 포함 — 기준이 0이면 0보다 큰 승률이 무조건 '높음'으로 분류되는 버그 방지.)
    position_stats = _position_mixture_baseline(all_df, player_df, metrics_for_baseline)

    player_position_df = player_df[player_df["position"] == position] if position != "UNKNOWN" else player_df
    player_position_stats = {
        metric: _mean(player_position_df[metric])
        for metric in metrics_for_baseline
        if metric in player_position_df.columns
    }
    player_champ_stats = _compute_group_means(player_df, "champion_id", metrics_for_baseline)
    champ_weights = player_df.groupby("champion_id").size()
    player_champ_weighted: dict[str, float] = {}
    for metric in metrics_for_baseline:
        total_weight = 0.0
        weighted_sum = 0.0
        for champion_id, games in champ_weights.items():
            player_champ = player_champ_stats.get(champion_id, {})
            val = player_champ.get(metric)
            if val is not None:
                weighted_sum += val * games
                total_weight += games
        player_champ_weighted[metric] = weighted_sum / total_weight if total_weight else player_position_stats.get(metric, 0.0)

    player_stats = _player_aggregate(player_df)

    if player_stats["matches"] < min_games:
        return {
            "error": f"분석에 필요한 최소 경기 수({min_games}판) 미달",
            "player_id": player_id,
            "matches": int(player_stats["matches"]),
        }

    summary_rows = _build_summary_rows(player_stats, position_stats, player_champ_weighted)
    position_breakdown = _position_breakdown(
        all_df, player_df, min_position_games=min_position_games
    )

    strengths, weaknesses = _rank_strengths_weaknesses(player_stats, position_stats)
    win_loss = _win_loss_analysis(player_df)
    champion = _champion_analysis(player_df)
    style = _play_style_classification(player_stats, position_stats)

    win_patterns = _build_pattern_lines(win_loss["comparisons"], positive=True)
    loss_patterns = _build_pattern_lines(
        sorted(win_loss["comparisons"], key=lambda item: item["impact"]),
        positive=False,
    )

    # 신규 분석 섹션 — 데이터가 없으면 각 함수가 None을 반환해 자연히 생략된다.
    name_map = _build_player_name_map(all_df)
    ping_profile = _ping_profile(player_df)
    lane_performance = _lane_performance(player_df)
    lane_final = _lane_final_analysis(player_df, all_df)
    recent_form = _recent_form(player_df)
    synergy = _synergy_analysis(all_df, player_id, name_map)
    momentum = _momentum_analysis(player_df, match_timelines)

    pos_service = _get_position_service()
    return {
        "player_id": player_id,
        "matches": int(player_stats["matches"]),
        "main_position": position,
        "main_position_label": pos_service.position_label(position),
        "sub_position": sub_position,
        "sub_position_label": pos_service.position_label(sub_position),
        "summary_table": summary_rows,
        "position_breakdown": position_breakdown,
        "strengths_top3": [
            {
                "label": item["label"],
                "desc": _metric_desc(item["metric"]),
                "value": _format_metric(item["metric"], item["value"]),
                "position_avg": _format_metric(item["metric"], item["position_avg"]),
                "rating": item["display_rating"],
                "description": f"포지션 평균 대비 {item['display_rating']} (평균 {_format_metric(item['metric'], item['position_avg'])})",
            }
            for item in strengths
        ],
        "weaknesses_top3": [
            {
                "label": item["label"],
                "desc": _metric_desc(item["metric"]),
                "value": _format_metric(item["metric"], item["value"]),
                "position_avg": _format_metric(item["metric"], item["position_avg"]),
                "rating": item["display_rating"],
                "description": f"포지션 평균 대비 {item['display_rating']} (평균 {_format_metric(item['metric'], item['position_avg'])})",
            }
            for item in weaknesses
        ],
        "win_patterns": win_patterns,
        "loss_patterns": loss_patterns,
        "win_condition": _build_win_condition_text(
            win_loss["win_condition"],
            insufficient_sample=win_loss.get("insufficient_sample", False),
        ),
        "recommended_style": style["recommended_style"],
        "player_type": style["player_type_label"],
        "player_type_scores": style["scores"],
        "one_liner": _build_one_liner(player_stats, style, win_loss["win_condition"], strengths),
        "champion_analysis": champion,
        "win_loss_detail": win_loss["comparisons"],
        "ping_profile": ping_profile,
        "lane_performance": lane_performance,
        "lane_final_analysis": lane_final,
        "recent_form": recent_form,
        "synergy": synergy,
        "momentum": momentum,
    }
