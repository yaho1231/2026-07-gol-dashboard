import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import pandas as pd
from app.config import settings
from app.database import engine, get_db, Base
from app.models.match import Match, MatchParticipant
from app.models.player import Player, PlayerDiscordAlias
from app.schemas.schemas import (
    PlayerCreate, PlayerUpdate, PlayerOut,
    RiotAccountCreate, RiotAccountOut,
    MatchOut, MatchmakeRequest, MatchmakeResponse, MatchmakeCommentaryRequest,
    PlayerAnalysisReport,
)
from app.services import player_service
from app.services.matchmaking import balance_teams
from app.services.ai_service import ai_service
from app.services.player_analysis import analyze_player
from app.services.champion_data import resolve_champion_name
from app.services.match_report import build_match_report
from app.services.mmr_service import migrate_all_players_mmr_from_tier
from app.services import records_service
from app.services.weekly_report import build_weekly_report
from app.services.style_card import render_style_card
from app.services.share_cards import (
    render_duo_card,
    render_hall_of_fame_card,
    render_match_mvp_card,
    render_momentum_card,
    render_power_ranking_card,
    render_roster_card,
)
from app.workers.match_sync import (
    sync_all_registered_accounts,
    relink_participants_to_accounts,
    MANUAL_SYNC_MATCH_COUNT,
    AUTO_SYNC_MATCH_COUNT,
)
# is_syncing 은 동기화 중 재할당되는 모듈 플래그라, 값으로 import 하면 안 되고
# 모듈을 통해 참조해야 최신 상태를 읽는다. (match_sync.is_syncing)
from app.workers import match_sync

# Set up Async scheduler for background sync tasks
scheduler = AsyncIOScheduler()


class _MutePollingAccessLog(logging.Filter):
    """봇이 15초/60초마다 치는 상태 폴링 요청을 uvicorn 접근 로그에서 제외한다.
    콘솔이 폴링 로그로 도배되는 걸 막되, 등록·동기화·에러 등 나머지 요청 로그는
    그대로 남긴다. (기능에는 영향 없음)"""

    _MUTED_PATHS = ("/api/sync/status", "/api/matches/reports")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access 레코드 args = (client, method, full_path, http_version, status)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = str(args[2]).split("?", 1)[0]
            if path in self._MUTED_PATHS:
                return False
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 봇의 주기적 폴링(/api/sync/status·/api/matches/reports) 접근 로그를 콘솔에서 숨긴다.
    # uvicorn 이 로깅을 먼저 구성한 뒤 lifespan 이 실행되므로 여기서 붙여야 유지된다.
    _access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _MutePollingAccessLog) for f in _access_logger.filters):
        _access_logger.addFilter(_MutePollingAccessLog())

    # 1. Initialize DB Tables if they don't exist
    Base.metadata.create_all(bind=engine)
    print("Database tables initialized.")

    # Dynamic migrations for missing columns in SQLite
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            cursor = conn.execute(text("PRAGMA table_info(riot_accounts);"))
            columns = [row[1] for row in cursor.fetchall()]
            if "solo_tier" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN solo_tier VARCHAR;"))
                print("Added column solo_tier to riot_accounts table.")
            if "solo_division" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN solo_division INTEGER;"))
                print("Added column solo_division to riot_accounts table.")
            if "solo_league_points" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN solo_league_points INTEGER;"))
                print("Added column solo_league_points to riot_accounts table.")
            if "solo_high_tier" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN solo_high_tier VARCHAR;"))
                print("Added column solo_high_tier to riot_accounts table.")
            if "solo_tier_updated_at" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN solo_tier_updated_at DATETIME;"))
                print("Added column solo_tier_updated_at to riot_accounts table.")
            if "last_seen_match_id" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN last_seen_match_id VARCHAR;"))
                print("Added column last_seen_match_id to riot_accounts table.")
            if "last_synced_at" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN last_synced_at DATETIME;"))
                print("Added column last_synced_at to riot_accounts table.")
            if "last_active_at" not in columns:
                conn.execute(text("ALTER TABLE riot_accounts ADD COLUMN last_active_at DATETIME;"))
                print("Added column last_active_at to riot_accounts table.")

            cursor = conn.execute(text("PRAGMA table_info(matches);"))
            match_columns = [row[1] for row in cursor.fetchall()]
            if "raw_data" not in match_columns:
                conn.execute(text("ALTER TABLE matches ADD COLUMN raw_data TEXT;"))
                print("Added column raw_data to matches table.")

            cursor = conn.execute(text("PRAGMA table_info(match_participants);"))
            participant_columns = [row[1] for row in cursor.fetchall()]
            participant_migrations = {
                "cs": "INTEGER NOT NULL DEFAULT 0",
                "total_damage_dealt": "INTEGER NOT NULL DEFAULT 0",
                "total_damage_taken": "INTEGER NOT NULL DEFAULT 0",
                "damage_self_mitigated": "INTEGER NOT NULL DEFAULT 0",
                "total_heal": "INTEGER NOT NULL DEFAULT 0",
                "total_heals_on_teammates": "INTEGER NOT NULL DEFAULT 0",
                "total_damage_shielded_on_teammates": "INTEGER NOT NULL DEFAULT 0",
                "total_damage_dealt_to_objectives": "INTEGER NOT NULL DEFAULT 0",
                "total_damage_dealt_to_turrets": "INTEGER NOT NULL DEFAULT 0",
                "vision_score": "INTEGER NOT NULL DEFAULT 0",
                "wards_placed": "INTEGER NOT NULL DEFAULT 0",
                "wards_killed": "INTEGER NOT NULL DEFAULT 0",
                "control_wards_bought": "INTEGER NOT NULL DEFAULT 0",
                "turret_kills": "INTEGER NOT NULL DEFAULT 0",
                "inhibitor_kills": "INTEGER NOT NULL DEFAULT 0",
                "double_kills": "INTEGER NOT NULL DEFAULT 0",
                "triple_kills": "INTEGER NOT NULL DEFAULT 0",
                "quadra_kills": "INTEGER NOT NULL DEFAULT 0",
                "penta_kills": "INTEGER NOT NULL DEFAULT 0",
                "killing_sprees": "INTEGER NOT NULL DEFAULT 0",
                "largest_killing_spree": "INTEGER NOT NULL DEFAULT 0",
                "largest_multi_kill": "INTEGER NOT NULL DEFAULT 0",
                "champ_level": "INTEGER NOT NULL DEFAULT 0",
                "time_ccing_others": "INTEGER NOT NULL DEFAULT 0",
                "total_minions_killed": "INTEGER NOT NULL DEFAULT 0",
                "neutral_minions_killed": "INTEGER NOT NULL DEFAULT 0",
                "raw_data": "TEXT",
            }
            for column_name, column_type in participant_migrations.items():
                if column_name not in participant_columns:
                    conn.execute(text(f"ALTER TABLE match_participants ADD COLUMN {column_name} {column_type};"))
                    print(f"Added column {column_name} to match_participants table.")

            conn.execute(text("""
                INSERT OR IGNORE INTO player_discord_aliases (player_id, discord_user_id, display_name, created_at)
                SELECT id, discord_user_id, display_name, CURRENT_TIMESTAMP
                FROM players
                WHERE discord_user_id IS NOT NULL
            """))

            cursor = conn.execute(text("PRAGMA table_info(players);"))
            player_columns = [row[1] for row in cursor.fetchall()]
            if "initial_mmr" not in player_columns:
                conn.execute(text("ALTER TABLE players ADD COLUMN initial_mmr INTEGER NOT NULL DEFAULT 1200;"))
                print("Added column initial_mmr to players table.")
                conn.execute(text("UPDATE players SET initial_mmr = mmr WHERE initial_mmr = 1200;"))
    except Exception as e:
        print(f"Error executing SQLite Alter Table: {e}")

    # 기존 플레이어 MMR을 티어 기준으로 1회 마이그레이션
    migration_flag = Path(__file__).resolve().parent.parent / ".mmr_tier_migration_v1"
    try:
        from app.database import SessionLocal
        if not migration_flag.exists():
            db = SessionLocal()
            try:
                count = migrate_all_players_mmr_from_tier(db)
                migration_flag.write_text("done")
                print(f"Migrated MMR for {count} players based on current tier.")
            finally:
                db.close()
    except Exception as e:
        print(f"MMR migration skipped or failed: {e}")

    # 2. Configure & Start Scheduler
    scheduler.add_job(
        sync_all_registered_accounts,
        trigger="interval",
        minutes=settings.SYNC_INTERVAL_MINUTES,
        id="match_sync_job",
        kwargs={"match_count": AUTO_SYNC_MATCH_COUNT},
    )
    scheduler.start()
    print(f"Background Match Sync Scheduler started (Interval: {settings.SYNC_INTERVAL_MINUTES} mins).")

    # 3. Trigger initial sync in the background so we don't block start-up
    asyncio.create_task(sync_all_registered_accounts(match_count=AUTO_SYNC_MATCH_COUNT))

    yield
    # Shutdown
    scheduler.shutdown()
    print("Scheduler shut down.")

app = FastAPI(
    title="GOL League Inhouse Manager API",
    version="1.0.0",
    lifespan=lifespan
)

# CORS: 이 API를 브라우저에서 직접 부르는 건 대시보드뿐이라 출처를 좁혀둔다.
# 다른 출처에서 붙여야 하면 .env CORS_ALLOW_ORIGINS 에 콤마로 구분해 추가한다.
# allow_credentials=False — 쿠키/인증정보를 실어 보낼 필요가 없고,
# 와일드카드 출처와 credentials 를 함께 켜는 조합을 피한다.
_default_origins = [
    f"http://localhost:{settings.DASHBOARD_PORT}",
    f"http://127.0.0.1:{settings.DASHBOARD_PORT}",
]
if settings.PUBLIC_URL:
    _default_origins.append(settings.PUBLIC_URL.rstrip("/"))
_extra_origins = [o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_origins + _extra_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# --- 보안: 읽기(GET)는 공개, 쓰기(POST/PUT/DELETE)는 내부 요청 또는 API 키 필요 ---
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_internal_request(request: Request) -> bool:
    client = request.client
    if client is None or client.host not in ("127.0.0.1", "::1"):
        return False
    # cloudflared 터널을 거친 외부 요청도 127.0.0.1에서 들어오므로,
    # Cloudflare가 붙이는 헤더가 있으면 외부 요청으로 취급한다.
    return "cf-connecting-ip" not in request.headers


@app.middleware("http")
async def require_api_key_for_writes(request: Request, call_next):
    if request.method in SAFE_METHODS or _is_internal_request(request):
        return await call_next(request)
    provided = request.headers.get("x-api-key", "")
    if settings.API_KEY and secrets.compare_digest(provided, settings.API_KEY):
        return await call_next(request)
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "쓰기 작업에는 유효한 X-API-Key 헤더가 필요합니다."},
    )

@app.get("/", include_in_schema=False)
def root(request: Request):
    # 접속한 호스트 그대로 대시보드 포트로 안내 (로컬/공개 도메인 모두 동작)
    return RedirectResponse(f"http://{request.url.hostname}:{settings.DASHBOARD_PORT}/")


# --- Player Endpoints ---
@app.post("/api/players", response_model=PlayerOut, status_code=status.HTTP_201_CREATED)
def create_player(player: PlayerCreate, db: Session = Depends(get_db)):
    # Check duplicate Discord User ID
    if player.discord_user_id:
        existing = player_service.get_player_by_discord(db, player.discord_user_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A player with this Discord User ID already exists."
            )
    return player_service.create_player(db, player)

@app.get("/api/players", response_model=list[PlayerOut])
def list_players(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return player_service.get_players(db, skip=skip, limit=limit)

@app.get("/api/players/discord/{discord_id}", response_model=PlayerOut)
def get_player_by_discord(discord_id: str, db: Session = Depends(get_db)):
    db_player = player_service.get_player_by_discord(db, discord_id)
    if not db_player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with Discord ID {discord_id} not found."
        )
    return db_player

@app.get("/api/players/{player_id}", response_model=PlayerOut)
def get_player(player_id: int, db: Session = Depends(get_db)):
    db_player = player_service.get_player(db, player_id)
    if not db_player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with ID {player_id} not found."
        )
    return db_player

@app.put("/api/players/{player_id}", response_model=PlayerOut)
def update_player(player_id: int, player: PlayerUpdate, db: Session = Depends(get_db)):
    updated_player = player_service.update_player(db, player_id, player)
    if not updated_player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with ID {player_id} not found."
        )
    return updated_player

@app.delete("/api/players/{player_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_player(player_id: int, db: Session = Depends(get_db)):
    success = player_service.delete_player(db, player_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with ID {player_id} not found."
        )
    return


def _load_participants_dataframe(db: Session) -> pd.DataFrame:
    rows = (
        db.query(MatchParticipant, Match.game_duration, Match.game_creation, Player.display_name)
        .join(Match, Match.match_id == MatchParticipant.match_id)
        .outerjoin(Player, Player.id == MatchParticipant.player_id)
        .all()
    )
    records = []
    for participant, game_duration, game_creation, display_name in rows:
        record = {column.name: getattr(participant, column.name) for column in MatchParticipant.__table__.columns}
        record["game_duration"] = game_duration
        record["game_creation"] = game_creation
        record["display_name"] = display_name
        records.append(record)
    return pd.DataFrame(records)


def _load_match_timelines_for_player(db: Session, player_id: int) -> dict[str, dict]:
    """해당 플레이어가 참여한 경기들의 time_analysis(분당 승률 타임라인)를 모은다.

    경기 raw_data는 용량이 커서, 분석 대상 플레이어의 경기만 골라 파싱한다."""
    import json

    rows = (
        db.query(Match.match_id, Match.raw_data)
        .join(MatchParticipant, MatchParticipant.match_id == Match.match_id)
        .filter(MatchParticipant.player_id == player_id)
        .distinct()
        .all()
    )
    timelines: dict[str, dict] = {}
    for match_id, raw in rows:
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        ta = payload.get("time_analysis")
        if ta:
            timelines[match_id] = ta
    return timelines


@app.get("/api/players/{player_id}/analysis", response_model=PlayerAnalysisReport)
def get_player_analysis(player_id: int, db: Session = Depends(get_db)):
    db_player = player_service.get_player(db, player_id)
    if not db_player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with ID {player_id} not found.",
        )

    df_participants = _load_participants_dataframe(db)
    match_timelines = _load_match_timelines_for_player(db, player_id)
    report = analyze_player(player_id, df_participants, match_timelines=match_timelines)
    if "error" in report:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=report["error"],
        )
    return report

# --- Riot Account Endpoints ---
@app.post("/api/players/{player_id}/accounts", response_model=RiotAccountOut)
async def link_riot_account(
    player_id: int, 
    account: RiotAccountCreate, 
    db: Session = Depends(get_db)
):
    linked_account = await player_service.link_riot_account(db, player_id, account)
    if not linked_account:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not verify or link Riot Account via DeepLOL API. Make sure the Riot Game Name & Tag are exact."
        )
    relink_participants_to_accounts(db)
    return linked_account

@app.delete("/api/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_riot_account(account_id: int, db: Session = Depends(get_db)):
    success = player_service.unlink_riot_account(db, account_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Riot Account with ID {account_id} not found."
        )
    return

# --- Match Endpoints ---
@app.get("/api/matches", response_model=list[MatchOut])
def list_matches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    matches = db.query(Match).order_by(Match.game_creation.desc()).offset(skip).limit(limit).all()
    for match in matches:
        for participant in match.participants:
            participant.champion_name = resolve_champion_name(
                participant.champion_id,
                participant.champion_name,
            )
    return matches

@app.get("/api/matches/reports")
def get_match_reports(
    within_minutes: int | None = None,
    limit: int = 5,
    db: Session = Depends(get_db),
):
    """경기 리포트(팀 스탯 + 어워드 + 흐름) 목록 — 최신 경기부터.

    within_minutes 를 주면 '그 시간 안에 종료된' 경기만 남긴다
    (봇의 경기 자동 포스팅이 30분 창으로 폴링). limit=1 이면 /최근경기 용.
    """
    import time as _time

    limit = max(1, min(limit, 20))
    now = _time.time()
    reports = []
    for m in db.query(Match).order_by(Match.game_creation.desc()).limit(50).all():
        end_epoch = (m.game_creation or 0) / 1000 + (m.game_duration or 0)
        if within_minutes is not None and (now - end_epoch) > within_minutes * 60:
            continue
        rows = (
            db.query(MatchParticipant, Player.display_name)
            .outerjoin(Player, Player.id == MatchParticipant.player_id)
            .filter(MatchParticipant.match_id == m.match_id)
            .all()
        )
        participants = []
        for participant, display_name in rows:
            p = {c.name: getattr(participant, c.name) for c in MatchParticipant.__table__.columns}
            p["display_name"] = display_name
            p["champion_name"] = resolve_champion_name(participant.champion_id, participant.champion_name)
            participants.append(p)
        report = build_match_report(
            {c.name: getattr(m, c.name) for c in Match.__table__.columns},
            participants,
        )
        if report:
            # 이 경기에서 갱신된 서버 신기록/멀티킬 이벤트 — 봇 임베드의 🚨 필드.
            try:
                report["new_records"] = records_service.new_records_for_match(db, m.match_id)
            except Exception:
                report["new_records"] = []
            reports.append(report)
        if len(reports) >= limit:
            break
    return {"reports": reports}


@app.get("/api/hall-of-fame")
def get_hall_of_fame(db: Session = Depends(get_db)):
    """명예의 전당 — 단일 경기 기록 보유자 + 누적 MVP/에이스/다승/출전 보드."""
    return records_service.build_hall_of_fame(db)


@app.get("/api/hall-of-fame/card")
def get_hall_of_fame_card(db: Session = Depends(get_db)):
    """명예의 전당 카드 PNG — /명예의전당 명령이 첨부 이미지로 올린다."""
    png = render_hall_of_fame_card(db)
    if png is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="아직 기록이 없습니다.")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/matches/latest/momentum-card")
def get_latest_momentum_card(db: Session = Depends(get_db)):
    """가장 최근 경기의 흐름(모멘텀) 그래프 카드 PNG — /경기흐름 명령용."""
    png = render_momentum_card(db)
    if png is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="타임라인 데이터가 있는 경기가 없습니다.",
        )
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/duo-card")
def get_duo_card(player_a: int, player_b: int, db: Session = Depends(get_db)):
    """듀오 시너지 카드 PNG — /듀오카드 명령이 첨부 이미지로 올린다."""
    try:
        png = render_duo_card(db, player_a, player_b)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/matchmake/roster-card")
def get_roster_card(payload: MatchmakeResponse, db: Session = Depends(get_db)):
    """팀 매칭 결과를 로스터 배너 PNG로 — /팀생성 결과 메시지에 첨부된다."""
    png = render_roster_card(db, payload.model_dump())
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/matches/{match_id}/mvp-card")
def get_match_mvp_card(match_id: str, db: Session = Depends(get_db)):
    """경기 MVP 카드 PNG — 경기 자동 리포트와 /최근경기 임베드에 첨부된다."""
    png = render_match_mvp_card(db, match_id)
    if png is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MVP 카드를 만들 수 없는 경기입니다.",
        )
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/power-ranking/card")
def get_power_ranking_card(db: Session = Depends(get_db)):
    """파워랭킹 카드 PNG — /파워랭킹 명령이 첨부 이미지로 올린다."""
    png = render_power_ranking_card(db)
    if png is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="랭킹에 올릴 플레이어가 아직 없습니다.",
        )
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/reports/weekly")
def get_weekly_report(days: int = 7, db: Session = Depends(get_db)):
    """주간 리포트 — 봇이 매주 자동 발행하고 /주간리포트 명령도 같은 데이터를 쓴다."""
    days = max(1, min(days, 31))
    return build_weekly_report(db, days=days)


@app.get("/api/players/{player_id}/style-card")
def get_player_style_card(player_id: int, db: Session = Depends(get_db)):
    """플레이스타일 카드 PNG — /스타일카드 명령이 첨부 이미지로 올린다."""
    db_player = player_service.get_player(db, player_id)
    if not db_player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player with ID {player_id} not found.",
        )
    df_participants = _load_participants_dataframe(db)
    report = analyze_player(player_id, df_participants)
    if "error" in report:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=report["error"])
    png = render_style_card(report, db_player.display_name or f"Player {player_id}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.delete("/api/matches/{match_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_match(match_id: str, db: Session = Depends(get_db)):
    db_match = db.query(Match).filter(Match.match_id == match_id).first()
    if not db_match:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Match with ID {match_id} not found."
        )
    db.delete(db_match)
    db.commit()
    return

@app.post("/api/relink", status_code=status.HTTP_200_OK)
def trigger_relink(db: Session = Depends(get_db)):
    linked_count = relink_participants_to_accounts(db)
    return {"relinked_participants": linked_count}

@app.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync():
    """수동 동기화 — 최근 20경기를 전 계정 빠짐없이(thorough) 조회."""
    asyncio.create_task(
        sync_all_registered_accounts(match_count=MANUAL_SYNC_MATCH_COUNT, thorough=True)
    )
    return {"status": "Sync process triggered in the background.", "match_count": MANUAL_SYNC_MATCH_COUNT}

@app.get("/api/sync/status")
async def sync_status():
    """봇이 폴링해 '경기 데이터 수집중...' 상태 표시 여부를 판단한다."""
    return {"is_syncing": match_sync.is_syncing}

# --- Matchmaking Endpoint ---
@app.post("/api/matchmake", response_model=MatchmakeResponse)
async def matchmake(request: MatchmakeRequest, db: Session = Depends(get_db)):
    """10명을 팀으로 분할. AI 해설은 별도 /api/matchmake/commentary 엔드포인트에서 요청."""
    teams = balance_teams(
        db,
        discord_ids=request.discord_user_ids,
        balance_mode=request.balance_mode,
        option_lane_priority=request.option_lane_priority,
        option_mmr_balance=request.option_mmr_balance,
        option_recent_form=request.option_recent_form,
        option_duo_separation=request.option_duo_separation,
        duo_pairs=request.duo_pairs,
    )

    if not teams:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Matchmaking failed. Ensure exactly 10 registered Discord users are provided.",
        )

    return teams


@app.post("/api/matchmake/commentary")
async def matchmake_commentary(request: MatchmakeCommentaryRequest):
    """매치업 표시 후 별도로 호출하는 AI 경기 예측 해설."""
    teams_dict = {
        "blue_team": request.blue_team,
        "red_team": request.red_team,
        "mmr_difference": request.mmr_difference,
        "balance_mode": request.balance_mode,
    }
    commentary = await ai_service.generate_match_commentary(teams_dict)
    return {"ai_commentary": commentary}
