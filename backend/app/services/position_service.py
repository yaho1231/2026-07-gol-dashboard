"""포지션 정규화 · 최신 가중 주/부 포지션 산정 · 한글 라벨.

내전 데이터의 포지션 처리를 한 곳에서 담당한다.
- DeepLOL 원본 포지션 문자열(Top/Jungle/Mid/Bot/Supporter 등)을 5개 표준값으로 정규화.
- 최근 경기일수록 더 큰 가중치를 줘서 주/부 포지션이 실시간으로 바뀌도록 산정.
- 표준값(TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY)을 한글(탑/정글/미드/원딜/서폿)로 변환.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

# DB에 저장되는 표준 포지션 값.
CANONICAL_POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

POSITION_KR = {
    "TOP": "탑",
    "JUNGLE": "정글",
    "MIDDLE": "미드",
    "BOTTOM": "원딜",
    "UTILITY": "서폿",
    "UNKNOWN": "미정",
}

# 원본 → 표준. 표준값을 소문자로 넣어도 자기 자신으로 매핑되도록 구성(idempotent).
# DeepLOL은 서폿을 "Supporter"로 내려주는데, 과거엔 "Support"만 처리해 서폿이 전부
# UNKNOWN으로 저장되는 버그가 있었다. 약어/한글/원딜 표기까지 폭넓게 흡수한다.
_RAW_POSITION_MAP = {
    # TOP
    "top": "TOP", "탑": "TOP",
    # JUNGLE
    "jungle": "JUNGLE", "jng": "JUNGLE", "jg": "JUNGLE", "정글": "JUNGLE",
    # MIDDLE
    "mid": "MIDDLE", "middle": "MIDDLE", "미드": "MIDDLE",
    # BOTTOM (원딜)
    "bot": "BOTTOM", "bottom": "BOTTOM", "adc": "BOTTOM", "ad": "BOTTOM",
    "carry": "BOTTOM", "duo_carry": "BOTTOM", "bottom_carry": "BOTTOM",
    "원딜": "BOTTOM", "바텀": "BOTTOM",
    # UTILITY (서폿)
    "support": "UTILITY", "supporter": "UTILITY", "sup": "UTILITY",
    "utility": "UTILITY", "duo_support": "UTILITY",
    "서폿": "UTILITY", "서포터": "UTILITY", "서포트": "UTILITY",
}


def normalize_position(raw: object) -> str:
    """임의의 포지션 표기를 표준값으로 변환한다. 모르는 값은 UNKNOWN."""
    if raw is None:
        return "UNKNOWN"
    key = str(raw).strip().lower()
    if not key:
        return "UNKNOWN"
    return _RAW_POSITION_MAP.get(key, "UNKNOWN")


def position_label(position: object) -> str:
    """표준 포지션 값을 한글 라벨로 변환한다(탑/정글/미드/원딜/서폿)."""
    return POSITION_KR.get(str(position or "UNKNOWN").upper(), "미정")


def rank_positions(
    positions_newest_first: Sequence[object],
    *,
    decay: float = 0.85,
) -> list[tuple[str, float]]:
    """최신 경기일수록 큰 가중치를 줘서 포지션별 점수를 매긴다.

    positions_newest_first[0] 이 가장 최근 경기의 포지션이어야 한다.
    가중치는 최신부터 1, decay, decay^2 ... 로 감쇠한다.
    UNKNOWN 은 점수에서 제외하되, 경기 순번(감쇠)은 그대로 소비한다.
    반환: 점수 내림차순 (표준포지션, 점수) 목록.
    """
    weights: dict[str, float] = defaultdict(float)
    factor = 1.0
    for raw in positions_newest_first:
        canon = normalize_position(raw)
        if canon != "UNKNOWN":
            weights[canon] += factor
        factor *= decay
    return sorted(weights.items(), key=lambda item: item[1], reverse=True)


def main_and_sub_position(
    positions_newest_first: Sequence[object],
    *,
    decay: float = 0.85,
) -> tuple[str, str]:
    """최신 가중 기준 (주포지션, 부포지션)을 반환한다. 없으면 UNKNOWN."""
    ranked = rank_positions(positions_newest_first, decay=decay)
    main = ranked[0][0] if ranked else "UNKNOWN"
    sub = ranked[1][0] if len(ranked) > 1 else "UNKNOWN"
    return main, sub
