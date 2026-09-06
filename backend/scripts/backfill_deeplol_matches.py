import argparse
import json
import ssl
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.champion_data import resolve_champion_name

BASE_URL = "https://b2c-api-cdn.deeplol.gg"
INHOUSE_QUEUE_IDS = {0, 3130}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.deeplol.gg",
    "Referer": "https://www.deeplol.gg/",
}
SSL_CONTEXT = ssl._create_unverified_context()


def fetch_json(url: str) -> dict | None:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"Fetch failed: {url} ({exc})")
        return None


def fetch_match_list(puuid: str, count: int) -> list[str]:
    params = urllib.parse.urlencode({
        "puu_id": puuid,
        "platform_id": "KR",
        "offset": 0,
        "count": count,
        "queue_type": "ALL",
        "champion_id": 0,
        "only_list": 1,
        "last_updated_at": 0,
    })
    data = fetch_json(f"{BASE_URL}/match/matches?{params}")
    if not data:
        return []

    match_ids = []
    for entry in data.get("match_id_list", []):
        if isinstance(entry, dict) and entry.get("match_id"):
            match_ids.append(str(entry["match_id"]))
        elif isinstance(entry, str):
            match_ids.append(entry)
    return match_ids


def fetch_match_detail(match_id: str) -> dict | None:
    params = urllib.parse.urlencode({"match_id": match_id, "platform_id": "KR"})
    return fetch_json(f"{BASE_URL}/match/match-cached?{params}")


def get_int_stat(participant: dict, final_stats: dict, *keys: str, default: int = 0) -> int:
    value = None
    for key in keys:
        if participant.get(key) is not None:
            value = participant[key]
            break
    if value is None:
        for key in keys:
            if final_stats.get(key) is not None:
                value = final_stats[key]
                break
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def normalize_position(position: str) -> str:
    return {
        "Top": "TOP",
        "Jungle": "JUNGLE",
        "Mid": "MIDDLE",
        "Middle": "MIDDLE",
        "Bot": "BOTTOM",
        "Support": "UTILITY",
        "top": "TOP",
        "jungle": "JUNGLE",
        "mid": "MIDDLE",
        "middle": "MIDDLE",
        "bot": "BOTTOM",
        "support": "UTILITY",
        "TOP": "TOP",
        "JUNGLE": "JUNGLE",
        "MIDDLE": "MIDDLE",
        "BOTTOM": "BOTTOM",
        "UTILITY": "UTILITY",
    }.get(position, "UNKNOWN")


def recalculate_player_mmr(conn: sqlite3.Connection, player_id: int) -> None:
    rows = conn.execute(
        """
        SELECT mp.win, mp.ai_score
        FROM match_participants mp
        JOIN matches m ON mp.match_id = m.match_id
        WHERE mp.player_id = ?
        ORDER BY m.game_creation ASC
        """,
        (player_id,),
    ).fetchall()
    if not rows:
        return

    mmr = 1200
    for win, ai_score in rows:
        ai_norm = float(ai_score or 50.0) / 10.0
        if win:
            ai_bonus = max(0.0, min(5.0, (ai_norm - 5.0) * 1.5))
            mmr += int(15 + ai_bonus)
        else:
            mitigation = max(0.0, min(8.0, (ai_norm - 6.0) * 2))
            mmr -= int(15 - mitigation)
    conn.execute("UPDATE players SET mmr = ? WHERE id = ?", (max(100, mmr), player_id))


def insert_match(conn: sqlite3.Connection, match_id: str, match_data: dict) -> bool:
    if conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone():
        return False

    basic = match_data.get("match_basic_dict") or {}
    participants = match_data.get("participants_list") or []
    if not basic or not participants:
        print(f"Skipping {match_id}: missing match data")
        return False

    queue_id = basic.get("queue_id", -1)
    if queue_id not in INHOUSE_QUEUE_IDS:
        return False

    creation_timestamp = basic.get("creation_timestamp", time.time())
    game_creation = int(creation_timestamp * 1000) if creation_timestamp < 10000000000 else int(creation_timestamp)
    affected_players = set()

    conn.execute(
        """
        INSERT INTO matches (match_id, game_creation, game_duration, game_mode, synced_at, raw_data)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            game_creation,
            int(basic.get("game_duration") or 0),
            "CUSTOM",
            datetime.utcnow().isoformat(sep=" "),
            json.dumps(match_data, ensure_ascii=False),
        ),
    )

    for participant in participants:
        final_stats = participant.get("final_stat_dict") or {}
        puuid = participant.get("puu_id")
        riot_name = participant.get("riot_id_name") or participant.get("summoner_name") or "Unknown"
        riot_tag = participant.get("riot_id_tag_line") or "KR1"
        account = None
        if puuid:
            account = conn.execute(
                "SELECT id, player_id FROM riot_accounts WHERE puuid = ?",
                (puuid,),
            ).fetchone()

        riot_account_id = account[0] if account else None
        player_id = account[1] if account else None
        if player_id:
            affected_players.add(player_id)
            conn.execute(
                "UPDATE riot_accounts SET riot_game_name = ?, riot_tag = ? WHERE id = ?",
                (riot_name, riot_tag, riot_account_id),
            )

        kills = get_int_stat(participant, final_stats, "kills")
        deaths = get_int_stat(participant, final_stats, "deaths")
        assists = get_int_stat(participant, final_stats, "assists")
        kda = final_stats.get("kda")
        if kda is None:
            kda = (kills + assists) / (deaths if deaths > 0 else 1)

        conn.execute(
            """
            INSERT INTO match_participants (
                match_id, riot_account_id, player_id, summoner_name, riot_tag,
                champion_id, champion_name, win, kills, deaths, assists, kda, cs,
                total_damage_dealt, total_damage_dealt_to_champions, total_damage_taken, damage_self_mitigated,
                total_heal, total_heals_on_teammates, total_damage_shielded_on_teammates,
                total_damage_dealt_to_objectives, total_damage_dealt_to_turrets,
                gold_earned, vision_score, wards_placed, wards_killed, control_wards_bought,
                turret_kills, inhibitor_kills, double_kills, triple_kills, quadra_kills,
                penta_kills, killing_sprees, largest_killing_spree, largest_multi_kill,
                champ_level, time_ccing_others, total_minions_killed, neutral_minions_killed,
                ai_score, team_id, position, raw_data
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                match_id,
                riot_account_id,
                player_id,
                riot_name,
                riot_tag,
                int(participant.get("champion_id") or 0),
                resolve_champion_name(int(participant.get("champion_id") or 0)),
                bool(participant.get("is_win", False)),
                kills,
                deaths,
                assists,
                float(kda),
                get_int_stat(participant, final_stats, "cs", "total_cs", "total_minions_killed", "totalMinionsKilled"),
                get_int_stat(participant, final_stats, "total_damage_dealt", "totalDamageDealt"),
                get_int_stat(participant, final_stats, "total_damage_dealt_to_champion", "total_damage_dealt_to_champions", "totalDamageDealtToChampions"),
                get_int_stat(participant, final_stats, "total_damage_taken", "totalDamageTaken"),
                get_int_stat(participant, final_stats, "damage_self_mitigated", "damageSelfMitigated"),
                get_int_stat(participant, final_stats, "total_heal", "totalHeal"),
                get_int_stat(participant, final_stats, "total_heals_on_teammates", "totalHealsOnTeammates"),
                get_int_stat(participant, final_stats, "total_damage_shielded_on_teammates", "totalDamageShieldedOnTeammates"),
                get_int_stat(participant, final_stats, "total_damage_dealt_to_objectives", "damage_dealt_to_objectives", "damageDealtToObjectives"),
                get_int_stat(participant, final_stats, "total_damage_dealt_to_turrets", "damage_dealt_to_turrets", "damageDealtToTurrets"),
                get_int_stat(participant, final_stats, "total_gold", "gold_earned", "goldEarned"),
                get_int_stat(participant, final_stats, "vision_score", "visionScore"),
                get_int_stat(participant, final_stats, "wards_placed", "wardsPlaced"),
                get_int_stat(participant, final_stats, "wards_killed", "wardsKilled"),
                get_int_stat(participant, final_stats, "control_wards_bought", "vision_wards_bought_in_game", "visionWardsBoughtInGame"),
                get_int_stat(participant, final_stats, "turret_kills", "turretKills"),
                get_int_stat(participant, final_stats, "inhibitor_kills", "inhibitorKills"),
                get_int_stat(participant, final_stats, "double_kills", "doubleKills"),
                get_int_stat(participant, final_stats, "triple_kills", "tripleKills"),
                get_int_stat(participant, final_stats, "quadra_kills", "quadraKills"),
                get_int_stat(participant, final_stats, "penta_kills", "pentaKills"),
                get_int_stat(participant, final_stats, "killing_sprees", "killingSprees"),
                get_int_stat(participant, final_stats, "largest_killing_spree", "largestKillingSpree"),
                get_int_stat(participant, final_stats, "largest_multi_kill", "largestMultiKill"),
                get_int_stat(participant, final_stats, "champ_level", "champion_level", "champLevel"),
                get_int_stat(participant, final_stats, "time_ccing_others", "timeCCingOthers"),
                get_int_stat(participant, final_stats, "total_minions_killed", "totalMinionsKilled"),
                get_int_stat(participant, final_stats, "neutral_minions_killed", "neutralMinionsKilled"),
                float(participant.get("ai_score") or final_stats.get("ai_score") or 50.0),
                100 if participant.get("side", "BLUE") == "BLUE" else 200,
                normalize_position(participant.get("position", "UNKNOWN")),
                json.dumps(participant, ensure_ascii=False),
            ),
        )

    for player_id in affected_players:
        recalculate_player_mmr(conn, player_id)
    print(f"Imported inhouse match {match_id} (queue_id={queue_id})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "inhouse.db"))
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--match-id", action="append", default=[])
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        match_ids = list(args.match_id)
        if not match_ids:
            accounts = conn.execute(
                "SELECT riot_game_name, riot_tag, puuid FROM riot_accounts WHERE verified = 1"
            ).fetchall()
            for name, tag, puuid in accounts:
                ids = fetch_match_list(puuid, args.count)
                print(f"Found {len(ids)} recent matches for {name}#{tag}")
                match_ids.extend(ids)

        imported = 0
        seen = set()
        for match_id in match_ids:
            if match_id in seen or conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone():
                continue
            seen.add(match_id)
            detail = fetch_match_detail(match_id)
            if detail and insert_match(conn, match_id, detail):
                imported += 1
                conn.commit()
            time.sleep(args.sleep)
        print(f"Imported {imported} new inhouse matches.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
