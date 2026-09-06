import re
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from app.models.player import Player, PlayerDiscordAlias
from app.models.riot_account import RiotAccount
from app.models.match import MatchParticipant
from app.schemas.schemas import PlayerCreate, PlayerUpdate, RiotAccountCreate
from app.services.deeplol_api import deeplol_client, parse_solo_rank_from_realtime
from app.services.mmr_service import apply_tier_mmr_to_player
from app.config import settings

def get_player(db: Session, player_id: int) -> Optional[Player]:
    return db.query(Player).filter(Player.id == player_id).first()

def normalize_display_name(display_name: str) -> str:
    normalized = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", display_name or "").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized or display_name

def get_player_by_discord(db: Session, discord_user_id: str) -> Optional[Player]:
    player = db.query(Player).filter(Player.discord_user_id == discord_user_id).first()
    if player:
        return player

    alias = db.query(PlayerDiscordAlias).filter(
        PlayerDiscordAlias.discord_user_id == discord_user_id
    ).first()
    return alias.player if alias else None

def get_player_by_display_name(db: Session, display_name: str) -> Optional[Player]:
    normalized = normalize_display_name(display_name)
    return db.query(Player).filter(Player.display_name == normalized).first()

def add_discord_alias(
    db: Session,
    player_id: int,
    discord_user_id: Optional[str],
    display_name: Optional[str] = None
) -> None:
    if not discord_user_id:
        return

    existing = db.query(PlayerDiscordAlias).filter(
        PlayerDiscordAlias.discord_user_id == discord_user_id
    ).first()
    if existing:
        existing.player_id = player_id
        existing.display_name = display_name
        return

    db.add(PlayerDiscordAlias(
        player_id=player_id,
        discord_user_id=discord_user_id,
        display_name=display_name
    ))

def get_players(db: Session, skip: int = 0, limit: int = 100) -> List[Player]:
    return db.query(Player).offset(skip).limit(limit).all()

def create_player(db: Session, player: PlayerCreate) -> Player:
    normalized_name = normalize_display_name(player.display_name)
    existing_by_discord = get_player_by_discord(db, player.discord_user_id) if player.discord_user_id else None
    if existing_by_discord:
        existing_by_discord.display_name = normalized_name
        existing_by_discord.discord_user_id = player.discord_user_id
        add_discord_alias(db, existing_by_discord.id, player.discord_user_id, player.display_name)
        db.commit()
        db.refresh(existing_by_discord)
        return existing_by_discord

    db_player = Player(
        display_name=normalized_name,
        discord_user_id=player.discord_user_id,
        mmr=player.mmr
    )
    db.add(db_player)
    db.flush()
    add_discord_alias(db, db_player.id, player.discord_user_id, player.display_name)
    db.commit()
    db.refresh(db_player)
    return db_player

def update_player(db: Session, player_id: int, player: PlayerUpdate) -> Optional[Player]:
    db_player = get_player(db, player_id)
    if not db_player:
        return None
    
    update_data = player.model_dump(exclude_unset=True)
    if "display_name" in update_data and update_data["display_name"]:
        update_data["display_name"] = normalize_display_name(update_data["display_name"])
    for key, value in update_data.items():
        setattr(db_player, key, value)
    add_discord_alias(db, db_player.id, db_player.discord_user_id, db_player.display_name)
        
    db.commit()
    db.refresh(db_player)
    return db_player

def delete_player(db: Session, player_id: int) -> bool:
    db_player = get_player(db, player_id)
    if not db_player:
        return False
    account_ids = [account.id for account in db_player.accounts]
    if account_ids:
        db.query(MatchParticipant).filter(
            MatchParticipant.riot_account_id.in_(account_ids)
        ).update({MatchParticipant.riot_account_id: None}, synchronize_session=False)
    db.query(MatchParticipant).filter(
        MatchParticipant.player_id == player_id
    ).update({MatchParticipant.player_id: None}, synchronize_session=False)
    db.delete(db_player)
    db.commit()
    return True

def extract_puuid_and_summoner_id(data: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """
    Robust helper to parse puu_id and summoner_id from DeepLOL summoner lookup payload.
    """
    puu_id = None
    summoner_id = None
    
    # Check root level keys
    for k in ["puu_id", "puuid", "g_puuid", "puuId"]:
        if k in data and data[k]:
            puu_id = str(data[k])
            break
            
    for k in ["summoner_id", "summonerId", "id"]:
        if k in data and data[k]:
            summoner_id = str(data[k])
            break

    # Check nested dictionaries commonly found in DeepLOL responses
    nested_keys = ["summoner_basic_info_dict", "data", "summoner"]
    for nk in nested_keys:
        if nk in data and isinstance(data[nk], dict):
            nested = data[nk]
            if not puu_id:
                for k in ["puu_id", "puuid", "g_puuid", "puuId"]:
                    if k in nested and nested[k]:
                        puu_id = str(nested[k])
                        break
            if not summoner_id:
                for k in ["summoner_id", "summonerId", "id"]:
                    if k in nested and nested[k]:
                        summoner_id = str(nested[k])
                        break
                    
    return puu_id, summoner_id

def apply_solo_rank_to_account(account: RiotAccount, rank_info: Dict[str, Any]) -> None:
    if rank_info.get("solo_tier"):
        account.solo_tier = rank_info["solo_tier"]
        account.solo_division = rank_info.get("solo_division")
        account.solo_league_points = rank_info.get("solo_league_points")
        account.solo_high_tier = rank_info.get("solo_high_tier")
    account.solo_tier_updated_at = datetime.utcnow()

def tier_refresh_needed(account: RiotAccount) -> bool:
    if not account.solo_tier_updated_at:
        return True
    refresh_after = timedelta(hours=settings.TIER_REFRESH_HOURS)
    return datetime.utcnow() - account.solo_tier_updated_at >= refresh_after

def account_rank_snapshot(account: RiotAccount) -> Dict[str, Any]:
    return {
        "solo_tier": account.solo_tier,
        "solo_division": account.solo_division,
        "solo_league_points": account.solo_league_points,
        "solo_high_tier": account.solo_high_tier,
    }

async def refresh_account_solo_rank(account: RiotAccount, force: bool = False) -> Optional[Dict[str, Any]]:
    if not force and not tier_refresh_needed(account):
        return None

    realtime_data = await deeplol_client.get_summoner_realtime(
        account.puuid,
        account.summoner_id or "",
    )
    rank_info = parse_solo_rank_from_realtime(realtime_data)
    apply_solo_rank_to_account(account, rank_info)
    return rank_info

async def link_riot_account(
    db: Session, 
    player_id: int, 
    account_in: RiotAccountCreate
) -> Optional[RiotAccount]:
    """
    Queries DeepLOL to fetch summoner details (puu_id), links it to the Player, 
    and sets as main if requested.
    """
    db_player = get_player(db, player_id)
    if not db_player:
        return None
        
    # Search for summoner info via DeepLOL Client
    summoner_data = await deeplol_client.get_summoner_info(
        account_in.riot_game_name, 
        account_in.riot_tag
    )
    if not summoner_data:
        return None
        
    puu_id, summoner_id = extract_puuid_and_summoner_id(summoner_data)
    if not puu_id:
        print(f"Could not resolve puu_id from summoner info response: {summoner_data}")
        return None
        
    # Check if this puu_id is already linked
    existing = db.query(RiotAccount).filter(RiotAccount.puuid == puu_id).first()
    if existing:
        if account_in.main_account:
            db.query(RiotAccount).filter(
                RiotAccount.player_id == player_id
            ).update({RiotAccount.main_account: False})
        # Re-associate or fail
        existing.player_id = player_id
        existing.riot_game_name = account_in.riot_game_name
        existing.riot_tag = account_in.riot_tag
        existing.main_account = account_in.main_account
        existing.verified = True
        if not existing.summoner_id and summoner_id:
            existing.summoner_id = summoner_id
        await refresh_account_solo_rank(existing, force=True)
        from app.models.match import MatchParticipant
        has_matches = (
            db.query(MatchParticipant)
            .filter(MatchParticipant.player_id == player_id)
            .count()
            > 0
        )
        if not has_matches:
            player = get_player(db, player_id)
            if player:
                apply_tier_mmr_to_player(player)
        db.commit()
        db.refresh(existing)
        return existing
        
    # If this is set to main_account, demote existing main accounts for this player
    if account_in.main_account:
        db.query(RiotAccount).filter(
            RiotAccount.player_id == player_id
        ).update({RiotAccount.main_account: False})
        
    new_account = RiotAccount(
        player_id=player_id,
        riot_game_name=account_in.riot_game_name,
        riot_tag=account_in.riot_tag,
        puuid=puu_id,
        summoner_id=summoner_id,
        main_account=account_in.main_account,
        verified=True,
    )
    db.add(new_account)
    db.flush()
    await refresh_account_solo_rank(new_account, force=True)

    from app.models.match import MatchParticipant
    has_matches = (
        db.query(MatchParticipant)
        .filter(MatchParticipant.player_id == player_id)
        .count()
        > 0
    )
    if not has_matches:
        apply_tier_mmr_to_player(db_player)

    db.commit()
    db.refresh(new_account)
    return new_account

def unlink_riot_account(db: Session, account_id: int) -> bool:
    db_account = db.query(RiotAccount).filter(RiotAccount.id == account_id).first()
    if not db_account:
        return False
    db.delete(db_account)
    db.commit()
    return True
