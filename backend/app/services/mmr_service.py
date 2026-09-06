from typing import Optional
from sqlalchemy.orm import Session
from app.models.player import Player
from app.models.riot_account import RiotAccount

# 1200 = 골드~플래티넘 경계 구간
TIER_BASE_MMR = {
    "CHALLENGER": 2100,
    "GRANDMASTER": 1950,
    "MASTER": 1800,
    "DIAMOND": 1650,
    "EMERALD": 1450,
    "PLATINUM": 1270,
    "GOLD": 1120,
    "SILVER": 950,
    "BRONZE": 780,
    "IRON": 620,
    "UNRANKED": 1200,
}

TIER_RANK = {
    "IRON": 1,
    "BRONZE": 2,
    "SILVER": 3,
    "GOLD": 4,
    "PLATINUM": 5,
    "EMERALD": 6,
    "DIAMOND": 7,
    "MASTER": 8,
    "GRANDMASTER": 9,
    "CHALLENGER": 10,
    "UNRANKED": 4,
}

DEFAULT_MMR = 1200


def convert_tier_to_mmr(tier: str, division: Optional[int]) -> int:
    tier = str(tier or "UNRANKED").upper()
    base = TIER_BASE_MMR.get(tier, DEFAULT_MMR)

    if tier not in ("CHALLENGER", "GRANDMASTER", "MASTER", "UNRANKED") and division:
        adj = {1: 50, 2: 20, 3: -20, 4: -50}
        base += adj.get(division, 0)

    return base


def convert_tier_to_rank(tier: str, division: Optional[int]) -> float:
    """티어 기반 밸런싱용 연속 랭크 값 (티어 모드)."""
    tier = str(tier or "UNRANKED").upper()
    base = float(TIER_RANK.get(tier, 4))
    if tier not in ("CHALLENGER", "GRANDMASTER", "MASTER", "UNRANKED") and division:
        base += (5 - division) * 0.2
    return base


def get_main_account(player: Player) -> Optional[RiotAccount]:
    if not player.accounts:
        return None
    return next((acc for acc in player.accounts if acc.main_account), player.accounts[0])


def get_player_tier_mmr(player: Player) -> int:
    main_acc = get_main_account(player)
    if main_acc and main_acc.solo_tier:
        return convert_tier_to_mmr(main_acc.solo_tier, main_acc.solo_division)
    return DEFAULT_MMR


def get_player_tier_rank(player: Player) -> float:
    main_acc = get_main_account(player)
    if main_acc and main_acc.solo_tier:
        return convert_tier_to_rank(main_acc.solo_tier, main_acc.solo_division)
    return convert_tier_to_rank("UNRANKED", None)


def apply_tier_mmr_to_player(player: Player, set_initial: bool = True) -> int:
    mmr = get_player_tier_mmr(player)
    player.mmr = mmr
    if set_initial:
        player.initial_mmr = mmr
    return mmr


def get_player_base_mmr(player: Player) -> int:
    return player.initial_mmr if player.initial_mmr else get_player_tier_mmr(player)


def migrate_all_players_mmr_from_tier(db: Session) -> int:
    """기존 등록 플레이어 MMR을 현재 티어 기준으로 재설정."""
    players = db.query(Player).all()
    for player in players:
        player.initial_mmr = get_player_tier_mmr(player)
    db.commit()

    # 티어 시작값이 바뀌었으니 전체 Elo 리플레이로 현재 MMR을 다시 만든다.
    from app.workers.match_sync import recalculate_all_mmr
    recalculate_all_mmr(db)
    return len(players)
