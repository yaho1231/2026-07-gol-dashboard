import asyncio
import json
import time
from datetime import datetime, timedelta
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.player import Player
from app.models.riot_account import RiotAccount
from app.models.match import Match, MatchParticipant
from app.services.champion_data import resolve_champion_name
from app.services.deeplol_api import deeplol_client
from app.services.player_service import refresh_account_solo_rank
from app.services.mmr_service import get_player_base_mmr
from app.services.position_service import normalize_position

INHOUSE_QUEUE_ID = 3130
AUTO_SYNC_MATCH_COUNT = 5
MANUAL_SYNC_MATCH_COUNT = 20
# 전적 갱신(refresh-matches) 요청 후 deeplol 서버에 반영될 때까지 잠시 대기한다.
REFRESH_WAIT_SECONDS = 2.0

# 적응형 자동 동기화 튜닝값
# - 최근 ACTIVE_WINDOW_DAYS 안에 새 경기가 있던 계정 = "활동" 계정 → 매 사이클 확인.
# - 그 외 "휴면" 계정은 DORMANT_CHECK_EVERY 사이클마다 한 번씩만 로테이션으로 확인한다.
ACTIVE_WINDOW_DAYS = 14
DORMANT_CHECK_EVERY = 6

# 자동 동기화 사이클 카운터(프로세스 수명 동안 유지). 휴면 계정 로테이션 분산에 쓰인다.
_auto_sync_cycle = 0

# 현재 동기화(수동/자동)가 진행 중인지 나타내는 플래그.
# 봇이 /api/sync/status 로 폴링해 "경기 데이터 수집중..." 상태 표시에 사용한다.
is_syncing = False

def get_nested_value(source: dict, *keys, default=0):
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return default

def get_int_stat(participant: dict, final_stats: dict, *keys, default=0) -> int:
    value = get_nested_value(participant, *keys, default=None)
    if value is None:
        value = get_nested_value(final_stats, *keys, default=default)
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _participant_puuid(participant: MatchParticipant) -> str | None:
    if not participant.raw_data:
        return None
    try:
        payload = json.loads(participant.raw_data)
    except json.JSONDecodeError:
        return None
    puuid = payload.get("puu_id") or payload.get("puuid")
    return str(puuid) if puuid else None


def relink_participants_to_accounts(db: Session) -> int:
    """
    Connect stored match participants to registered riot accounts.
    Used when matches were synced before players were registered.
    """
    accounts = db.query(RiotAccount).all()
    if not accounts:
        return 0

    puuid_map = {account.puuid: account for account in accounts if account.puuid}
    name_tag_map = {
        (account.riot_game_name.strip().lower(), account.riot_tag.strip().lower()): account
        for account in accounts
    }

    candidates = db.query(MatchParticipant).filter(
        or_(
            MatchParticipant.player_id.is_(None),
            MatchParticipant.riot_account_id.is_(None),
        )
    ).all()

    linked_count = 0
    affected_players: set[int] = set()

    for participant in candidates:
        account = None
        participant_puuid = _participant_puuid(participant)
        if participant_puuid:
            account = puuid_map.get(participant_puuid)

        if not account:
            name_tag_key = (
                participant.summoner_name.strip().lower(),
                participant.riot_tag.strip().lower(),
            )
            account = name_tag_map.get(name_tag_key)

        if not account:
            continue

        changed = False
        if participant.riot_account_id != account.id:
            participant.riot_account_id = account.id
            changed = True
        if participant.player_id != account.player_id:
            participant.player_id = account.player_id
            changed = True

        if changed:
            linked_count += 1
            affected_players.add(account.player_id)

    if linked_count:
        db.commit()
        recalculate_all_mmr(db)
        print(f"Relinked {linked_count} match participants to registered players.")

    return linked_count


# Elo K 계수 — 판수가 쌓일수록 변동 폭을 줄여 제자리를 찾은 뒤 안정화시킨다.
def _elo_k(games_played: int) -> float:
    if games_played < 5:
        return 44.0
    if games_played < 10:
        return 32.0
    return 24.0


# 개인 성과(팀 내 AI점수 상대치)가 증감에 미치는 최대 비율.
_PERF_CAP = 0.35


def compute_elo_ratings(
    db: Session, until_epoch_ms: int | None = None
) -> tuple[dict[int, float], dict[int, int]]:
    """전체 경기를 시간순으로 Elo 리플레이한 (레이팅, 판수) 를 반환한다. DB는 건드리지 않는다.

    until_epoch_ms 를 주면 그 시점 이전 경기까지만 리플레이한다 — 파워랭킹의
    "N일 전 대비 변동" 계산용.

    기존 방식(승리 고정 +15 + 각종 보너스)은 상대 전력을 보지 않아 잘하는 플레이어는
    무한 상승하고 약한 팀에 자주 걸린 플레이어는 계속 하락하는 문제가 있었다.
    - 기대 승률: 경기 시점 양 팀 평균 MMR의 Elo 기대값. 강팀으로 이기면 조금 오르고,
      약팀으로 지면 조금 깎여 평균에서 멀어질수록 증감이 자동으로 줄어든다 (수렴).
    - 개인 성과: 팀 내 AI점수 상대치로 증감을 ±35% 조정 — 진 팀의 캐리는 덜 깎이고,
      이긴 팀에 묻어간 경우는 덜 오른다 (팀운 보정).
    - 시작값은 티어 환산 MMR(initial_mmr), 미연동 참가자는 로비 평균으로 취급.
    """
    players = db.query(Player).all()
    ratings: dict[int, float] = {p.id: float(get_player_base_mmr(p)) for p in players}
    games_played: dict[int, int] = {p.id: 0 for p in players}

    query = db.query(Match).order_by(Match.game_creation.asc())
    if until_epoch_ms is not None:
        query = query.filter(Match.game_creation < until_epoch_ms)
    matches = query.all()
    for match in matches:
        teams: dict[int, list[MatchParticipant]] = {100: [], 200: []}
        for part in match.participants:
            if part.team_id in teams:
                teams[part.team_id].append(part)
        if not teams[100] or not teams[200]:
            continue

        known = [ratings[p.player_id] for side in teams.values() for p in side
                 if p.player_id in ratings]
        if not known:
            continue  # 갱신할 등록 플레이어가 없는 경기
        lobby_avg = sum(known) / len(known)

        def team_rating(side: list[MatchParticipant]) -> float:
            vals = [ratings.get(p.player_id, lobby_avg) if p.player_id is not None
                    else lobby_avg for p in side]
            return sum(vals) / len(vals)

        blue_rating = team_rating(teams[100])
        red_rating = team_rating(teams[200])
        expected_blue = 1.0 / (1.0 + 10 ** ((red_rating - blue_rating) / 400))

        # 같은 경기의 증감은 모두 경기 시작 시점 레이팅 기준으로 계산한 뒤 일괄 적용한다.
        deltas: dict[int, float] = {}
        for team_id, side in teams.items():
            ai_vals = [p.ai_score if p.ai_score is not None else 50.0 for p in side]
            team_ai = sum(ai_vals) / len(ai_vals) if ai_vals else 50.0
            expected = expected_blue if team_id == 100 else 1.0 - expected_blue
            for part in side:
                pid = part.player_id
                if pid not in ratings:
                    continue
                ai = part.ai_score if part.ai_score is not None else team_ai
                perf = 0.0
                if team_ai > 0:
                    perf = max(-_PERF_CAP, min(_PERF_CAP, (ai / team_ai - 1.0) * 1.5))
                score = 1.0 if part.win else 0.0
                delta = _elo_k(games_played[pid]) * (score - expected)
                # 승리 시 성과가 좋으면 더 얻고, 패배 시 성과가 좋으면 덜 잃는다.
                delta *= (1.0 + perf) if part.win else (1.0 - perf)
                deltas[pid] = delta

        for pid, delta in deltas.items():
            ratings[pid] += delta
            games_played[pid] += 1

    return ratings, games_played


def recalculate_all_mmr(db: Session) -> int:
    """Elo 리플레이 결과를 모든 플레이어의 MMR 컬럼에 반영한다."""
    ratings, _ = compute_elo_ratings(db)
    players = db.query(Player).all()
    for p in players:
        p.mmr = max(100, int(round(ratings[p.id])))
    db.commit()
    return len(players)


def recalculate_player_mmr_and_stats(db: Session, player_id: int):
    """하위 호환 래퍼 — Elo 재계산은 팀 상대 전력이 필요해 항상 전체를 리플레이한다."""
    recalculate_all_mmr(db)


def process_single_match(db: Session, match_id: str, match_data: dict) -> bool:
    """
    Parses and stores match details from the actual DeepLOL API response.
    
    Expected structure:
    {
        "match_basic_dict": {
            "match_id": "KR_xxx",
            "queue_id": 3130,         # 0/3130 = Custom/inhouse game
            "creation_timestamp": 1781446140.517,
            "game_duration": 1896,
            "blue_win": True/False,
            ...
        },
        "participants_list": [
            {
                "puu_id": "...",
                "riot_id_name": "...",
                "riot_id_tag_line": "KR1",
                "champion_id": 86,
                "side": "BLUE"/"RED",
                "position": "Top"/"Jungle"/"Mid"/"Bot"/"Support",
                "is_win": True/False,
                "ai_score": 61.24,
                "total_damage_dealt_to_champion": 150746,
                "total_gold": 13756,
                "final_stat_dict": {
                    "kills": 11,
                    "deaths": 8,
                    "assists": 5,
                    "kda": 2.0,
                    "cs": 201,
                    ...
                },
                ...
            },
            ...
        ]
    }
    """
    # Check if match already exists
    existing_match = db.query(Match).filter(Match.match_id == match_id).first()
    if existing_match:
        return False

    basic = match_data.get("match_basic_dict", {})
    participants_raw = match_data.get("participants_list", [])

    if not basic or not participants_raw:
        print(f"Skipping match {match_id}: missing basic or participants data")
        return False

    # 1. Verify inhouse custom game (queue_id 3130).
    queue_id = basic.get("queue_id", -1)
    if queue_id != INHOUSE_QUEUE_ID:
        return False

    # Extract metadata
    creation_timestamp = basic.get("creation_timestamp", time.time())
    # Convert seconds to milliseconds if needed
    if creation_timestamp < 10000000000:
        game_creation = int(creation_timestamp * 1000)
    else:
        game_creation = int(creation_timestamp)
    game_duration = basic.get("game_duration", 0)
    blue_win = basic.get("blue_win", False)

    # Instantiate Match
    db_match = Match(
        match_id=match_id,
        game_creation=game_creation,
        game_duration=game_duration,
        game_mode="CUSTOM",
        synced_at=datetime.utcnow(),
        raw_data=json.dumps(match_data, ensure_ascii=False)
    )
    db.add(db_match)

    # 2. Parse participants
    affected_players = set()

    for p in participants_raw:
        puuid = p.get("puu_id", None)
        riot_name = p.get("riot_id_name", "") or p.get("summoner_name", "Unknown")
        riot_tag = p.get("riot_id_tag_line", "KR1")
        champion_id = p.get("champion_id", 0)
        champion_name = resolve_champion_name(champion_id)

        # Side -> team_id mapping
        side = p.get("side", "BLUE")
        team_id = 100 if side == "BLUE" else 200

        # Win status
        is_win = p.get("is_win", False)

        # Stats from final_stat_dict
        final_stats = p.get("final_stat_dict", {})
        kills = final_stats.get("kills", 0)
        deaths = final_stats.get("deaths", 0)
        assists = final_stats.get("assists", 0)
        cs = get_int_stat(p, final_stats, "cs", "total_cs", "total_minions_killed", "totalMinionsKilled")
        kda = final_stats.get("kda", None)
        if kda is None:
            kda = (kills + assists) / (deaths if deaths > 0 else 1)

        # Damage and gold at root level
        total_damage_dealt = get_int_stat(
            p, final_stats,
            "total_damage_dealt",
            "totalDamageDealt",
        )
        damage_to_champions = get_int_stat(
            p, final_stats,
            "total_damage_dealt_to_champion",
            "total_damage_dealt_to_champions",
            "totalDamageDealtToChampions",
        )
        gold = get_int_stat(p, final_stats, "total_gold", "gold_earned", "goldEarned")
        total_damage_taken = get_int_stat(p, final_stats, "total_damage_taken", "totalDamageTaken")
        damage_self_mitigated = get_int_stat(p, final_stats, "damage_self_mitigated", "damageSelfMitigated")
        total_heal = get_int_stat(p, final_stats, "total_heal", "totalHeal")
        total_heals_on_teammates = get_int_stat(p, final_stats, "total_heals_on_teammates", "totalHealsOnTeammates")
        total_damage_shielded_on_teammates = get_int_stat(
            p, final_stats, "total_damage_shielded_on_teammates", "totalDamageShieldedOnTeammates"
        )
        objective_damage = get_int_stat(
            p, final_stats,
            "total_damage_dealt_to_objectives",
            "damage_dealt_to_objectives",
            "damageDealtToObjectives"
        )
        turret_damage = get_int_stat(
            p, final_stats,
            "total_damage_dealt_to_turrets",
            "damage_dealt_to_turrets",
            "damageDealtToTurrets"
        )
        vision_score = get_int_stat(p, final_stats, "vision_score", "visionScore")
        wards_placed = get_int_stat(p, final_stats, "wards_placed", "wardsPlaced")
        wards_killed = get_int_stat(p, final_stats, "wards_killed", "wardsKilled")
        control_wards_bought = get_int_stat(
            p, final_stats,
            "control_wards_bought",
            "vision_wards_bought",
            "vision_wards_bought_in_game",
            "visionWardsBoughtInGame"
        )
        turret_kills = get_int_stat(p, final_stats, "turret_kills", "turretKills")
        inhibitor_kills = get_int_stat(p, final_stats, "inhibitor_kills", "inhibitorKills")
        double_kills = get_int_stat(p, final_stats, "double_kills", "doubleKills")
        triple_kills = get_int_stat(p, final_stats, "triple_kills", "tripleKills")
        quadra_kills = get_int_stat(p, final_stats, "quadra_kills", "quadraKills")
        penta_kills = get_int_stat(p, final_stats, "penta_kills", "pentaKills")
        killing_sprees = get_int_stat(p, final_stats, "killing_sprees", "killingSprees")
        largest_killing_spree = get_int_stat(p, final_stats, "largest_killing_spree", "largestKillingSpree")
        largest_multi_kill = get_int_stat(p, final_stats, "largest_multi_kill", "largestMultiKill")
        champ_level = get_int_stat(p, final_stats, "champ_level", "champion_level", "champLevel")
        time_ccing_others = get_int_stat(p, final_stats, "time_ccing_others", "timeCCingOthers")
        total_minions_killed = get_int_stat(p, final_stats, "total_minions_killed", "totalMinionsKilled")
        neutral_minions_killed = get_int_stat(p, final_stats, "neutral_minions_killed", "neutralMinionsKilled")

        # AI Score (0-100 scale from DeepLOL)
        ai_score = p.get("ai_score", None)
        if ai_score is None:
            ai_score = min(100.0, max(10.0, (kills * 3 + assists * 2 - deaths * 2.5) * 2 + 50.0))

        # Position mapping — DeepLOL은 서폿을 "Supporter"로 내려주므로 robust 정규화 사용.
        position = normalize_position(p.get("position"))

        # Resolve against our registered Riot accounts
        riot_account_id = None
        player_id = None

        if puuid:
            account = db.query(RiotAccount).filter(RiotAccount.puuid == puuid).first()
            if account:
                riot_account_id = account.id
                player_id = account.player_id
                affected_players.add(player_id)
                # Keep account names updated
                if riot_name:
                    account.riot_game_name = riot_name
                if riot_tag:
                    account.riot_tag = riot_tag

        db_participant = MatchParticipant(
            match_id=match_id,
            riot_account_id=riot_account_id,
            player_id=player_id,
            summoner_name=riot_name,
            riot_tag=riot_tag,
            champion_id=champion_id,
            champion_name=champion_name,
            win=is_win,
            kills=kills,
            deaths=deaths,
            assists=assists,
            kda=kda,
            cs=cs,
            total_damage_dealt=total_damage_dealt,
            total_damage_dealt_to_champions=damage_to_champions,
            total_damage_taken=total_damage_taken,
            damage_self_mitigated=damage_self_mitigated,
            total_heal=total_heal,
            total_heals_on_teammates=total_heals_on_teammates,
            total_damage_shielded_on_teammates=total_damage_shielded_on_teammates,
            total_damage_dealt_to_objectives=objective_damage,
            total_damage_dealt_to_turrets=turret_damage,
            gold_earned=gold,
            vision_score=vision_score,
            wards_placed=wards_placed,
            wards_killed=wards_killed,
            control_wards_bought=control_wards_bought,
            turret_kills=turret_kills,
            inhibitor_kills=inhibitor_kills,
            double_kills=double_kills,
            triple_kills=triple_kills,
            quadra_kills=quadra_kills,
            penta_kills=penta_kills,
            killing_sprees=killing_sprees,
            largest_killing_spree=largest_killing_spree,
            largest_multi_kill=largest_multi_kill,
            champ_level=champ_level,
            time_ccing_others=time_ccing_others,
            total_minions_killed=total_minions_killed,
            neutral_minions_killed=neutral_minions_killed,
            ai_score=float(ai_score),
            team_id=team_id,
            position=position,
            raw_data=json.dumps(p, ensure_ascii=False)
        )
        db.add(db_participant)

    try:
        db.commit()
        print(f"Successfully synced custom match: {match_id}")

        # 새 경기가 들어오면 전체 Elo 리플레이로 모든 플레이어 MMR을 갱신한다.
        if affected_players:
            recalculate_all_mmr(db)

        return True
    except Exception as e:
        db.rollback()
        print(f"Failed to commit match {match_id} to DB: {e}")
        return False


def _extract_full_matches(matches_resp, limit: int) -> list[dict]:
    """only_list=0 응답의 `match_json_list`에서 경기 전체 데이터(dict) 목록을 뽑아낸다.
    각 항목은 dict 또는 JSON 문자열일 수 있어 모두 처리하고, 최신 limit개로 자른다."""
    if not isinstance(matches_resp, dict):
        return []
    raw = matches_resp.get("match_json_list") or []
    full_matches: list[dict] = []
    for entry in raw[:limit]:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except json.JSONDecodeError:
                continue
        if isinstance(entry, dict) and entry.get("match_basic_dict"):
            full_matches.append(entry)
    return full_matches


def _latest_match_id(matches_resp) -> str | None:
    """only_list=1 응답(`match_id_list`)에서 가장 최근 경기의 match_id 를 뽑는다.
    목록은 최신순이므로 첫 유효 항목이 최신 경기다. dict/문자열 항목 모두 처리한다."""
    if not isinstance(matches_resp, dict):
        return None
    for entry in matches_resp.get("match_id_list") or []:
        if isinstance(entry, dict) and entry.get("match_id"):
            return str(entry["match_id"])
        if isinstance(entry, str) and entry:
            return entry
    return None


def _registered_player_ids_in_match(db: Session, match_data: dict) -> set[int]:
    """경기 참가자(puuid) 중 우리 DB에 등록된 플레이어들의 player_id 집합을 반환한다."""
    puuids = [
        p.get("puu_id")
        for p in (match_data.get("participants_list") or [])
        if p.get("puu_id")
    ]
    if not puuids:
        return set()
    rows = (
        db.query(RiotAccount.player_id)
        .filter(RiotAccount.puuid.in_(puuids), RiotAccount.player_id.isnot(None))
        .all()
    )
    return {row[0] for row in rows}


def _due_for_dormant_check(account: RiotAccount, cycle_seq: int) -> bool:
    """휴면 계정 로테이션 판정: 계정마다 DORMANT_CHECK_EVERY 사이클에 한 번만 True.
    account.id 로 위상을 흩어 같은 사이클에 휴면 계정이 몰리지 않게 한다."""
    return cycle_seq % DORMANT_CHECK_EVERY == account.id % DORMANT_CHECK_EVERY


async def sync_all_registered_accounts(match_count: int = AUTO_SYNC_MATCH_COUNT, thorough: bool = False):
    """
    등록된 계정의 최근 경기를 동기화한다.

    효율화 설계
    - (0단계) 재크롤링: 게이트를 통과해 실제로 확인하는 계정은 자동 동기화에서도 refresh 를
      먼저 요청한다. deeplol 은 커스텀(내전) 경기를 스스로 색인하지 않는 경우가 많아,
      refresh 없이는 새 내전이 목록에 영영 안 나타날 수 있기 때문이다 (2026-07-11 실측).
    - (1단계) 변화 감지 게이트: refresh 후 가벼운 목록(only_list=1)만 읽어 최신 match_id 가
      지난번과 같으면 무거운 only_list=0 조회·파싱을 건너뛴다.
    - (2단계) 적응형 주기: 최근 ACTIVE_WINDOW_DAYS 안에 새 경기가 있던 "활동" 계정만 매
      사이클 확인하고, "휴면" 계정은 DORMANT_CHECK_EVERY 사이클마다 한 번씩만 로테이션으로
      확인한다 → 거의 안 하는 계정에 매번 호출하지 않는다.
    - 내전은 10명 전원이 등록 플레이어인 한 경기이며 그 한 건에 10명 데이터가 다 들어있다.
      따라서 한 내전을 발견하면 참가자 전원을 같은 사이클에서 재조회하지 않는다(coverage skip).
    - 새 경기가 감지되면 only_list=0 으로 전체 데이터(queue_id 포함)를 한 번에 받아 내전
      여부까지 추가 호출 없이 판별·수집한다.

    매개변수
    - match_count: 계정당 확인할 최근 경기 수 (자동 5 / 수동 20).
    - thorough=True(수동 동기화): 변화 감지·적응형 주기·coverage skip 없이 전수 조회.
    """
    global _auto_sync_cycle, is_syncing
    is_syncing = True
    db = SessionLocal()
    try:
        accounts = db.query(RiotAccount).filter(RiotAccount.verified == True).all()
        if not accounts:
            print("No verified Riot accounts found to sync.")
            return

        if not thorough:
            _auto_sync_cycle += 1
        cycle_seq = _auto_sync_cycle
        now = datetime.utcnow()
        active_cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)

        print(
            f"Starting Match Sync for {len(accounts)} accounts "
            f"(match_count={match_count}, thorough={thorough}, cycle={cycle_seq})..."
        )
        checked_match_ids: set[str] = set()
        covered_player_ids: set[int] = set()
        polled = 0
        skipped_covered = 0
        skipped_dormant = 0
        skipped_unchanged = 0

        for account in accounts:
            # 티어 갱신은 아래의 어떤 생략 게이트보다 먼저, 모든 계정에 대해 매 사이클 수행한다.
            # 게이트(covered/dormant/unchanged)는 "경기 목록"을 다시 안 읽으려는 최적화인데,
            # 여기에 티어까지 묶으면 정작 내전을 자주 뛰는 활성 플레이어(covered)의 티어가
            # 대시보드에서 며칠씩 옛날 값으로 남는다. 티어는 계정당 GET 1회로 싸다.
            try:
                await refresh_account_solo_rank(account, force=True)
                db.commit()
            except Exception as tier_err:
                print(f"Failed to refresh tier for {account.riot_game_name}#{account.riot_tag}: {tier_err}")

            # 같은 사이클에서 이미 발견된 내전에 참가했던 플레이어는 재조회 생략(자동 동기화 한정).
            if not thorough and account.player_id in covered_player_ids:
                skipped_covered += 1
                continue

            # (2단계) 적응형 주기: 휴면 계정은 매 사이클 보지 않고 로테이션으로만 확인.
            # 단, 한 번도 동기화한 적 없는 신규 계정은 즉시 1회 확인한다(콜드 스타트).
            if not thorough:
                is_active = account.last_active_at is not None and account.last_active_at >= active_cutoff
                never_synced = account.last_synced_at is None
                if not is_active and not never_synced and not _due_for_dormant_check(account, cycle_seq):
                    skipped_dormant += 1
                    continue

            prior_last_seen = account.last_seen_match_id
            latest_id = None

            # deeplol 은 커스텀(내전) 경기를 스스로 색인하지 않는 경우가 많아, 재크롤링 없이
            # 목록만 읽으면 새 내전이 영영 목록에 안 나타날 수 있다 (2026-07-11 실측: 밤 내전
            # 3경기가 8시간 동안 자동 동기화에 안 잡힘). 그래서 게이트를 통과해 실제로 확인하는
            # 계정은 자동/수동 구분 없이 refresh 를 먼저 요청한다.
            refreshed = await deeplol_client.refresh_matches(account.puuid, count=match_count)
            if refreshed:
                await asyncio.sleep(REFRESH_WAIT_SECONDS)

            if not thorough:
                # (1단계) 변화 감지: 가벼운 목록만 읽어 새 경기가 없으면 무거운 조회/파싱 생략.
                light_resp = await deeplol_client.get_match_list(
                    account.puuid, offset=0, count=match_count, only_list=1
                )
                latest_id = _latest_match_id(light_resp)
                if latest_id and latest_id == prior_last_seen:
                    # 지난번 확인 이후 새 경기 없음 → 무거운 조회/파싱 전부 생략.
                    account.last_synced_at = now
                    db.commit()
                    skipped_unchanged += 1
                    continue

            polled += 1
            print(f"Checking matches for account: {account.riot_game_name}#{account.riot_tag} ({account.puuid})")

            # only_list=0 → 경기 전체 데이터(queue_id 포함)를 한 번에 받는다.
            matches_resp = await deeplol_client.get_match_list(
                account.puuid, offset=0, count=match_count, only_list=0,
            )

            full_matches = _extract_full_matches(matches_resp, match_count) if matches_resp else []
            newest_full_id = None
            if full_matches:
                newest_full_id = (full_matches[0].get("match_basic_dict") or {}).get("match_id")

            # 다음 사이클의 변화 감지를 위해 최신 match_id 와 동기화 시각을 기록한다.
            account.last_seen_match_id = newest_full_id or latest_id or prior_last_seen
            account.last_synced_at = now
            # 새 경기가 실제로 있었으면 "활동" 계정으로 표시 → 다음부터 매 사이클 확인.
            if newest_full_id and newest_full_id != prior_last_seen:
                account.last_active_at = now

            print(f"Checking {len(full_matches)} recent matches on DeepLOL.")

            new_match_count = 0
            for match_data in full_matches:
                basic = match_data.get("match_basic_dict") or {}
                match_id = basic.get("match_id")
                if not match_id or match_id in checked_match_ids:
                    continue
                checked_match_ids.add(match_id)

                # 내전(커스텀)만 관심 대상 → 상세 조회 없이 인라인으로 즉시 판별.
                if basic.get("queue_id") != INHOUSE_QUEUE_ID:
                    continue

                # 이미 저장된 내전이면, 참가자만 covered 로 표시하고 넘어간다.
                if db.query(Match).filter(Match.match_id == match_id).first():
                    covered_player_ids |= _registered_player_ids_in_match(db, match_data)
                    continue

                if process_single_match(db, match_id, match_data):
                    new_match_count += 1
                    covered_player_ids |= _registered_player_ids_in_match(db, match_data)

            db.commit()
            print(f"Synced {new_match_count} new custom matches for {account.riot_game_name}.")
            # 계정 간 대기만 짧게 둔다(예의상 rate limit).
            await asyncio.sleep(1.0)

        print(
            f"Match sync (cycle {cycle_seq}): polled {polled}, "
            f"skipped {skipped_unchanged} unchanged / {skipped_dormant} dormant / {skipped_covered} covered."
        )
        relink_participants_to_accounts(db)

    except Exception as e:
        print(f"Error in sync_all_registered_accounts: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
        is_syncing = False
