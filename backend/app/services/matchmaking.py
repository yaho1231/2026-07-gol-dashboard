import itertools
from typing import List, Dict, Any, Optional, Literal
from sqlalchemy.orm import Session
from app.models.match import MatchParticipant, Match
from app.schemas.schemas import PlayerOut
from app.services.player_service import get_player_by_discord
from app.services.mmr_service import get_player_tier_rank
from app.services.position_service import main_and_sub_position

BalanceMode = Literal["mmr", "tier", "lane"]


def batch_get_player_stats(db: Session, player_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """플레이어 통계를 일괄 조회하여 DB 쿼리 수를 줄인다."""
    if not player_ids:
        return {}

    recent_rows = (
        db.query(MatchParticipant)
        .join(Match, MatchParticipant.match_id == Match.match_id)
        .filter(MatchParticipant.player_id.in_(player_ids))
        .order_by(Match.game_creation.desc())
        .all()
    )

    recent_by_player: Dict[int, list] = {pid: [] for pid in player_ids}
    for row in recent_rows:
        pid = row.player_id
        if pid in recent_by_player and len(recent_by_player[pid]) < 10:
            recent_by_player[pid].append(row)

    # 최신 경기가 먼저 오도록 정렬해, 주/부 포지션을 최신 가중으로 산정한다.
    position_rows = (
        db.query(MatchParticipant.player_id, MatchParticipant.position)
        .join(Match, MatchParticipant.match_id == Match.match_id)
        .filter(
            MatchParticipant.player_id.in_(player_ids),
            MatchParticipant.position.isnot(None),
            MatchParticipant.position != "UNKNOWN",
        )
        .order_by(Match.game_creation.desc())
        .all()
    )

    positions_by_player: Dict[int, list] = {pid: [] for pid in player_ids}
    for pid, position in position_rows:
        if pid in positions_by_player:
            positions_by_player[pid].append(position)

    result: Dict[int, Dict[str, Any]] = {}
    for pid in player_ids:
        recent = recent_by_player[pid]
        wins = sum(1 for p in recent if p.win)
        avg_ai = sum(p.ai_score for p in recent) / len(recent) if recent else 5.0
        win_rate = wins / len(recent) if recent else 0.5

        # positions_by_player[pid] 는 최신순. 최신 가중으로 주/부 포지션 결정.
        main_position, sub_position = main_and_sub_position(positions_by_player[pid])

        result[pid] = {
            "recent_win_rate": win_rate,
            "avg_ai_score": avg_ai,
            "main_position": main_position,
            "sub_position": sub_position,
        }
    return result


def _lane_duplicate_penalty(roles: List[str]) -> int:
    known = [r for r in roles if r != "UNKNOWN"]
    return len(known) - len(set(known))


def balance_teams(
    db: Session,
    discord_ids: List[str],
    balance_mode: BalanceMode = "mmr",
    option_lane_priority: bool = False,
    option_mmr_balance: bool = True,
    option_recent_form: bool = False,
    option_duo_separation: bool = False,
    duo_pairs: Optional[List[List[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    10명을 두 팀으로 분할. balance_mode에 따라 밸런싱 기준을 변경한다.
    - mmr: 평균 MMR 균형 (기본)
    - tier: 티어 랭크 중심, MMR 영향 축소
    - lane: 주 라인 중복 최소화 + MMR 균형
    """
    if balance_mode == "tier":
        option_mmr_balance = True
        option_lane_priority = False
        option_recent_form = False
    elif balance_mode == "lane":
        option_lane_priority = True
        option_mmr_balance = True
        option_recent_form = False
    else:
        option_lane_priority = option_lane_priority or True
        option_mmr_balance = True
        option_recent_form = option_recent_form or True

    selected_players = []
    player_by_selected_discord_id = {}
    seen_player_ids = set()
    for discord_id in discord_ids:
        player = get_player_by_discord(db, discord_id)
        if not player:
            continue
        if player.id in seen_player_ids:
            return None
        seen_player_ids.add(player.id)
        selected_players.append(player)
        player_by_selected_discord_id[discord_id] = player

    if len(selected_players) != 10:
        return None

    player_ids = [p.id for p in selected_players]
    stats_map = batch_get_player_stats(db, player_ids)

    player_data: Dict[str, Dict[str, Any]] = {}
    for discord_id, player in player_by_selected_discord_id.items():
        stats = stats_map.get(player.id, {
            "recent_win_rate": 0.5,
            "avg_ai_score": 5.0,
            "main_position": "UNKNOWN",
        })
        player_data[discord_id] = {
            "player": player,
            "mmr": player.mmr,
            "tier_rank": get_player_tier_rank(player),
            "main_position": stats["main_position"],
            "recent_win_rate": stats["recent_win_rate"],
            "avg_ai_score": stats["avg_ai_score"],
        }

    mmr_weight = {"mmr": 4.0, "tier": 1.0, "lane": 3.0}[balance_mode]
    tier_weight = {"mmr": 0.0, "tier": 5.0, "lane": 0.5}[balance_mode]
    lane_weight = {"mmr": 400.0, "tier": 200.0, "lane": 800.0}[balance_mode]
    form_weight = 1.5 if option_recent_form else 0.0

    first_player_id = discord_ids[0]
    other_player_ids = discord_ids[1:]

    best_combination = None
    min_penalty = float("inf")

    for comb in itertools.combinations(other_player_ids, 4):
        team1_ids = list(comb) + [first_player_id]
        team2_ids = [pid for pid in discord_ids if pid not in team1_ids]

        penalty = 0.0

        t1_mmrs = [player_data[pid]["mmr"] for pid in team1_ids]
        t2_mmrs = [player_data[pid]["mmr"] for pid in team2_ids]
        t1_avg_mmr = sum(t1_mmrs) / 5
        t2_avg_mmr = sum(t2_mmrs) / 5
        mmr_diff = abs(t1_avg_mmr - t2_avg_mmr)

        if option_mmr_balance:
            penalty += mmr_diff * mmr_weight

        if tier_weight > 0:
            t1_tier = sum(player_data[pid]["tier_rank"] for pid in team1_ids) / 5
            t2_tier = sum(player_data[pid]["tier_rank"] for pid in team2_ids) / 5
            penalty += abs(t1_tier - t2_tier) * tier_weight * 100

        if option_duo_separation and duo_pairs:
            for pair in duo_pairs:
                if len(pair) == 2:
                    in_t1 = pair[0] in team1_ids and pair[1] in team1_ids
                    in_t2 = pair[0] in team2_ids and pair[1] in team2_ids
                    if in_t1 or in_t2:
                        penalty += 10000.0

        if form_weight > 0:
            t1_form = sum(
                player_data[pid]["mmr"] * (0.5 + player_data[pid]["recent_win_rate"])
                + player_data[pid]["avg_ai_score"] * 50
                for pid in team1_ids
            ) / 5
            t2_form = sum(
                player_data[pid]["mmr"] * (0.5 + player_data[pid]["recent_win_rate"])
                + player_data[pid]["avg_ai_score"] * 50
                for pid in team2_ids
            ) / 5
            penalty += abs(t1_form - t2_form) * form_weight

        if option_lane_priority:
            t1_roles = [player_data[pid]["main_position"] for pid in team1_ids]
            t2_roles = [player_data[pid]["main_position"] for pid in team2_ids]
            penalty += (_lane_duplicate_penalty(t1_roles) + _lane_duplicate_penalty(t2_roles)) * lane_weight

        if penalty < min_penalty:
            min_penalty = penalty
            best_combination = (team1_ids, team2_ids, mmr_diff)

    if not best_combination:
        return None

    t1_ids, t2_ids, mmr_diff = best_combination

    t1_players = []
    for pid in t1_ids:
        player_out = PlayerOut.model_validate(player_data[pid]["player"])
        player_out.mmr = player_data[pid]["mmr"]
        t1_players.append(player_out)

    t2_players = []
    for pid in t2_ids:
        player_out = PlayerOut.model_validate(player_data[pid]["player"])
        player_out.mmr = player_data[pid]["mmr"]
        t2_players.append(player_out)

    t1_avg_mmr = sum(player_data[pid]["mmr"] for pid in t1_ids) / 5
    t2_avg_mmr = sum(player_data[pid]["mmr"] for pid in t2_ids) / 5

    mode_labels = {"mmr": "평균 MMR", "tier": "티어", "lane": "주 라인"}
    return {
        "blue_team": {
            "team_id": 100,
            "players": t1_players,
            "avg_mmr": t1_avg_mmr,
        },
        "red_team": {
            "team_id": 200,
            "players": t2_players,
            "avg_mmr": t2_avg_mmr,
        },
        "mmr_difference": mmr_diff,
        "balance_mode": balance_mode,
        "balance_mode_label": mode_labels.get(balance_mode, balance_mode),
    }
