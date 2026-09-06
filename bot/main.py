import os
import certifi

# macOS python.org 빌드는 시스템 CA 인증서 번들이 비어 있어 기본 SSL 검증이 실패한다.
# (예전 코드는 이를 가리려고 aiohttp SSL 검증을 통째로 껐지만, 이는 MITM 위험이 있다.)
# 검증을 끄는 대신, 신뢰할 수 있는 certifi CA 번들을 사용하도록 지정한다.
# discord.py(내부 aiohttp)가 SSL 컨텍스트를 만들기 전에 설정해야 하므로 import 보다 먼저 둔다.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import discord
from discord import app_commands
from discord.ext import commands, tasks
import httpx
import asyncio
import io
import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from bot.config import DISCORD_BOT_TOKEN, BACKEND_URL, ADMIN_DISCORD_IDS
from bot.views.participant_select import MatchmakeView
from bot.services.clash_api import generate_custom_code
from bot.services.deeplol_images import champion_image_url, refresh_champion_version
from bot import presence as presence_mgr
from bot import state as bot_state
from bot.presence import presence

# Define gateway intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
BOT_MESSAGE_DELETE_AFTER_SECONDS = 300
COMMANDS_SYNCED = False

# --- 봇 상태(Presence / "듣는 중") 관리 ---
# 상태 로직은 bot/presence.py 에 있다(views/* 와 순환 import 없이 공유하기 위함).
# 백엔드 동기화 진행 여부는 이 봇이 직접 모르므로(동기화는 백엔드에서 돈다),
# /api/sync/status 를 주기적으로 폴링해 '경기 데이터 수집중...' 상태를 켜고 끈다.
_sync_presence_token: int | None = None

@tasks.loop(hours=6)
async def refresh_champion_version_task():
    """DDragon 최신 챔피언 버전 캐시를 주기적으로 갱신한다(논블로킹).

    갱신 자체는 스레드에서 돌아 이벤트 루프를 막지 않는다. 이렇게 캐시를 항상 채워두면
    임베드 썸네일 생성 경로의 latest_champion_version() 이 네트워크를 탈 일이 없어,
    캐시 미스로 이벤트 루프가 얼어 인터랙션이 만료되는(10062) 문제를 막는다.
    """
    try:
        await refresh_champion_version()
    except Exception as e:
        print(f"[champion-version-refresh-error] {e!r}")


@tasks.loop(seconds=15)
async def poll_sync_status():
    """백엔드 동기화 진행 여부를 폴링해 '경기 데이터 수집중...' 상태를 켜고 끈다."""
    global _sync_presence_token
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{BACKEND_URL}/api/sync/status", timeout=5)
        syncing = resp.json().get("is_syncing", False)
    except Exception:
        return  # 백엔드 일시 장애 시 현재 상태 유지

    if syncing and _sync_presence_token is None:
        _sync_presence_token = await presence_mgr.acquire(
            "경기 데이터 수집중...", presence_mgr.PRIORITY_SYNC
        )
    elif not syncing and _sync_presence_token is not None:
        await presence_mgr.release(_sync_presence_token)
        _sync_presence_token = None

@poll_sync_status.before_loop
async def _before_poll_sync_status():
    await bot.wait_until_ready()


# --- 경기 자동 리포트 ---
# /코드 를 마지막으로 사용한 채널을 기억해 두고, 30분 이내에 끝난 경기가
# 백엔드에 새로 색인되면 경기 리포트를 그 채널에 자동으로 올린다.
RECENT_MATCH_WINDOW_MINUTES = 30


@tasks.loop(seconds=60)
async def poll_recent_matches():
    channel_id = bot_state.get_announce_channel_id()
    if not channel_id:
        return
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/matches/reports",
                params={"within_minutes": RECENT_MATCH_WINDOW_MINUTES, "limit": 5},
                timeout=20,
            )
        reports = resp.json().get("reports", [])
    except Exception:
        return  # 백엔드 일시 장애 시 다음 주기에 재시도

    new_reports = [r for r in reports if r.get("match_id") and not bot_state.is_announced(r["match_id"])]
    if not new_reports:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return

    for report in reversed(new_reports):  # 오래된 경기부터 순서대로
        try:
            file = await fetch_mvp_card_file(report["match_id"])
            if file:
                await channel.send(embed=format_match_report_embed(report), file=file)
            else:
                await channel.send(embed=format_match_report_embed(report))
            bot_state.mark_announced(report["match_id"])
            command_logger.info(f"AUTO-REPORT | match={report['match_id']} channel#{channel_id}")
        except Exception as e:
            print(f"Failed to post match report {report.get('match_id')}: {e}")


async def fetch_mvp_card_file(match_id: str) -> discord.File | None:
    """경기 MVP 카드 PNG를 받아 discord.File 로. 실패하면 None (임베드만 발행)."""
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/matches/{match_id}/mvp-card",
                timeout=60.0,  # 첫 호출은 스플래시 아트 다운로드로 수 초 걸릴 수 있다.
            )
        if resp.status_code == 200:
            return discord.File(io.BytesIO(resp.content), filename="gol_match_mvp.png")
    except Exception:
        pass
    return None


@poll_recent_matches.before_loop
async def _before_poll_recent_matches():
    await bot.wait_until_ready()


# --- 주간 리포트 자동 발행 ---
# 매주 월요일 저녁 8시(KST)에 지난 7일 결산을 공지 채널에 올린다.
# 봇이 월요일에 꺼져 있었어도 수요일까지는 따라잡아 발행한다(주 1회 보장).
KST = timezone(timedelta(hours=9))
WEEKLY_REPORT_TIME = dtime(hour=20, minute=0, tzinfo=KST)
WEEKLY_REPORT_LAST_WEEKDAY = 2  # 월(0)~수(2) 사이에만 발행


@tasks.loop(time=WEEKLY_REPORT_TIME)
async def post_weekly_report():
    now = datetime.now(KST)
    if now.weekday() > WEEKLY_REPORT_LAST_WEEKDAY:
        return
    iso = now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    if bot_state.get_last_weekly_report_week() == week_key:
        return
    channel_id = bot_state.get_announce_channel_id()
    if not channel_id:
        return

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{BACKEND_URL}/api/reports/weekly", params={"days": 7}, timeout=30)
        data = resp.json()
    except Exception:
        return  # 백엔드 일시 장애 시 다음 날 20시에 재시도

    if not data.get("games"):
        # 이번 주 내전이 없으면 조용히 넘어가되, 같은 주에 재시도하지 않는다.
        bot_state.set_last_weekly_report_week(week_key)
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return

    try:
        await channel.send(embed=format_weekly_report_embed(data))
        bot_state.set_last_weekly_report_week(week_key)
        command_logger.info(f"WEEKLY-REPORT | week={week_key} channel#{channel_id}")
    except Exception as e:
        print(f"Failed to post weekly report: {e}")


@post_weekly_report.before_loop
async def _before_post_weekly_report():
    await bot.wait_until_ready()

# --- 명령어 사용 기록 로깅 ---
# 누가 어떤 명령어를 사용했는지 이 컴퓨터의 파일로 남긴다.
# 프로젝트 루트의 logs/commands.log 에 기록되고, 콘솔(run.sh 터미널)에도 함께 출력된다.
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
COMMAND_LOG_PATH = os.path.join(LOG_DIR, "commands.log")

command_logger = logging.getLogger("inhouse.commands")
command_logger.setLevel(logging.INFO)
command_logger.propagate = False
if not command_logger.handlers:
    _file_handler = RotatingFileHandler(
        COMMAND_LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    command_logger.addHandler(_file_handler)

    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(
        logging.Formatter("[CMD] %(asctime)s | %(message)s", datefmt="%H:%M:%S")
    )
    command_logger.addHandler(_console_handler)


class _QuietGatewayReconnect(logging.Filter):
    """discord.py의 일시적 게이트웨이 재접속 ERROR(+전체 traceback)를 한 줄 WARNING으로 낮춘다.

    5xx 핸드셰이크 실패(예: 520)·소켓 끊김 등은 discord.py가 자동으로 RESUME/재접속해
    복구하므로, 매번 찍히는 스택트레이스는 실질 조치가 필요 없는 소음이다.
    (완전히 숨기지는 않고, 재접속이 일어난 사실은 한 줄로 남긴다.)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if "Attempting a reconnect" in record.getMessage():
            record.exc_info = None
            record.exc_text = None
            if record.levelno >= logging.ERROR:
                record.levelno = logging.WARNING
                record.levelname = "WARNING"
        return True


# discord.client 로거에 직접 로깅되는 재접속 이벤트에만 적용된다(자식 로거엔 영향 없음).
logging.getLogger("discord.client").addFilter(_QuietGatewayReconnect())


def _describe_command_options(data: dict) -> str:
    """인터랙션 원본 데이터에서 명령어 인자를 'name=value' 형태로 풀어낸다.
    멤버/유저 인자는 ID 뿐 아니라 표시 이름까지 함께 남긴다."""
    resolved_users = (data.get("resolved") or {}).get("users") or {}
    parts: list[str] = []

    def _walk(options):
        for opt in options or []:
            # 서브커맨드/그룹이면 한 단계 더 내려간다(현재는 없지만 방어적으로 처리).
            if opt.get("type") in (1, 2) and "options" in opt:
                _walk(opt.get("options"))
                continue
            name = opt.get("name")
            value = opt.get("value")
            if isinstance(value, str) and value in resolved_users:
                u = resolved_users[value]
                uname = u.get("global_name") or u.get("username") or value
                value = f"{uname}({value})"
            parts.append(f"{name}={value}")

    _walk(data.get("options"))
    return " ".join(parts)


def log_command_usage(interaction: discord.Interaction):
    """슬래시 명령 사용 1건을 파일/콘솔에 기록한다."""
    try:
        data = interaction.data or {}
        command_name = data.get("name", "?")
        user = interaction.user
        guild = interaction.guild
        guild_str = f"{guild.name}({guild.id})" if guild else "DM"
        channel_name = getattr(interaction.channel, "name", None) or "?"
        option_str = _describe_command_options(data)

        command_logger.info(
            f"/{command_name} | 사용자={user}({user.id}) | "
            f"서버={guild_str} | 채널=#{channel_name}"
            + (f" | 인자: {option_str}" if option_str else "")
        )
    except Exception as exc:  # 로깅 실패가 명령 실행을 막지 않도록 한다.
        print(f"[command-log-error] {exc!r}")


async def _tree_interaction_check(interaction: discord.Interaction) -> bool:
    # 모든 슬래시 명령 실행 직전에 호출된다 → 성공/실패와 무관하게 사용 기록을 남긴다.
    if interaction.type == discord.InteractionType.application_command:
        log_command_usage(interaction)
    return True


bot.tree.interaction_check = _tree_interaction_check

async def delete_message_later(message: discord.Message, delay: int = BOT_MESSAGE_DELETE_AFTER_SECONDS):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

async def send_response_autodelete(interaction: discord.Interaction, *args, **kwargs):
    await interaction.response.send_message(*args, **kwargs)
    # ephemeral(본인만 보이는) 응답까지 포함해, 오류·권한 안내 등 모든 응답을
    # 일정 시간 뒤 자동으로 정리한다.
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        return
    asyncio.create_task(delete_message_later(message))

async def send_followup_autodelete(interaction: discord.Interaction, *args, **kwargs):
    message = await interaction.followup.send(*args, **kwargs)
    # 인터랙션 followup 은 ephemeral 메시지도 항상 객체를 돌려주며 삭제가 가능하므로,
    # 오류·권한 안내(ephemeral 포함)도 동일하게 자동 삭제 대상에 포함한다.
    if message is not None:
        asyncio.create_task(delete_message_later(message))
    return message

async def notify_expired_interaction(interaction: discord.Interaction, command_name: str):
    """만료된(10062) 인터랙션 대신 채널에 재시도 안내를 남긴다.

    인터랙션 응답 API는 이미 쓸 수 없으므로 채널에 직접 보낸다. 권한이 없거나 채널을
    알 수 없으면 조용히 포기한다(안내 실패가 다시 예외로 번지지 않게).
    """
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        return
    try:
        message = await channel.send(
            f"⌛ {interaction.user.mention} `/{command_name}` 요청이 디스코드 응답 시한(3초)을 "
            "넘겨 처리되지 못했습니다. 잠시 후 다시 시도해주세요."
        )
    except discord.HTTPException:
        return
    asyncio.create_task(delete_message_later(message))


def is_admin_interaction(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        # DM 등 길드 밖에서는 관리자 개념이 없다.
        return False
    # 서버 소유자는 언제나 관리자로 취급한다.
    if guild.owner_id == getattr(interaction.user, "id", None):
        return True
    # 명시적 화이트리스트(.env ADMIN_DISCORD_IDS)에 등록된 유저는 서버 Administrator
    # 권한이 없어도 관리자로 취급한다. (예: 봇 운영자에게 관리 명령을 위임)
    if str(getattr(interaction.user, "id", "")) in ADMIN_DISCORD_IDS:
        return True
    # interaction.permissions 는 Discord 가 직접 계산해 인터랙션 페이로드로 함께 보내주는 값이라,
    # 대형 서버에서 멤버/역할 캐시가 비어 있거나 늦게 채워져도 항상 정확하다.
    # (interaction.user.guild_permissions 는 로컬 역할 캐시에 의존하므로, 캐시가 불완전한
    #  대형 서버에서는 관리자인데도 권한이 누락돼 보일 수 있다 → 이번 버그의 원인.)
    if interaction.permissions.administrator:
        return True
    # 폴백: 캐시가 채워져 있을 때만 정확한 멤버 역할 기반 권한.
    member_perms = getattr(interaction.user, "guild_permissions", None)
    return bool(member_perms and member_perms.administrator)

async def reject_non_admin(interaction: discord.Interaction) -> bool:
    if is_admin_interaction(interaction):
        return False
    message = "❌ 이 명령어는 서버 관리자만 사용할 수 있습니다."
    # 권한 부족 안내도 자동 삭제되도록 autodelete 헬퍼를 통해 전송한다.
    if interaction.response.is_done():
        await send_followup_autodelete(interaction, message, ephemeral=True)
    else:
        await send_response_autodelete(interaction, message, ephemeral=True)
    return True

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """슬래시 명령 처리 중 처리되지 않은 예외/권한 오류를 한곳에서 받아
    사용자에게 자동 삭제되는 안내 메시지를 보낸다."""
    command_name = interaction.command.name if interaction.command else "unknown"
    user = interaction.user

    # 인터랙션 만료/중복확인은 봇 잘못이 아니라 Discord 3초 시한을 넘긴 결과다.
    # (게이트웨이 재접속·호스트 절전 등으로 이벤트가 늦게 도착하면 defer() 조차 실패한다.)
    # 인터랙션 토큰은 이미 죽어 응답할 수 없지만, 그대로 두면 사용자에게는 봇이 씹은 것처럼
    # 보인다. 그래서 채널에 일반 메시지로 재시도 안내를 대신 남긴다(자동 삭제).
    original = getattr(error, "original", None)
    if isinstance(original, discord.NotFound) and getattr(original, "code", None) == 10062:
        command_logger.warning(
            f"인터랙션 만료(3초 시한 초과) /{command_name} | "
            f"사용자={user}({getattr(user, 'id', '?')}) — 채널 안내로 대체"
        )
        await notify_expired_interaction(interaction, command_name)
        return
    if isinstance(original, discord.HTTPException) and getattr(original, "code", None) == 40060:
        # 이미 응답된 인터랙션에 재응답 시도 — 무해하므로 조용히 넘긴다.
        return

    command_logger.error(
        f"명령 오류 /{command_name} | 사용자={user}({getattr(user, 'id', '?')}): {error!r}"
    )

    if isinstance(error, app_commands.CheckFailure):
        message = "❌ 이 명령어를 사용할 권한이 없습니다."
    else:
        message = "❌ 명령어 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    try:
        if interaction.response.is_done():
            await send_followup_autodelete(interaction, message, ephemeral=True)
        else:
            await send_response_autodelete(interaction, message, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.event
async def on_ready():
    global COMMANDS_SYNCED
    print(f"------")
    print(f"Logged in as Discord Bot: {bot.user.name} (ID: {bot.user.id})")
    print(f"API Backend URL: {BACKEND_URL}")

    # 기본 "듣는 중" 상태 표시 + 동기화 상태 폴링 시작 (재연결 시 중복 시작 방지)
    presence_mgr.set_client(bot)
    await presence_mgr.refresh()
    # 챔피언 버전 캐시를 즉시 1회 채워, 첫 임베드 생성이 네트워크를 타지 않게 한다.
    try:
        await refresh_champion_version()
    except Exception as e:
        print(f"[champion-version-refresh-error] {e!r}")
    if not refresh_champion_version_task.is_running():
        refresh_champion_version_task.start()
    if not poll_sync_status.is_running():
        poll_sync_status.start()
    if not poll_recent_matches.is_running():
        poll_recent_matches.start()
    if not post_weekly_report.is_running():
        post_weekly_report.start()

    if COMMANDS_SYNCED:
        print("Slash commands already synced for this bot session.")
        print(f"------")
        return
    
    # Sync slash commands to Discord.
    # 길드(guild) 단위 동기화 = 전파 지연 없이 '즉시' 반영된다. (이 봇은 사실상 단일 서버 운영)
    # 전역(global) 동기화는 최대 1시간까지 전파가 걸려, 권한/가시성 변경이 바로 안 보이는 문제가 있었다.
    # ── 여러 서버로 확장하면, 이 블록을 전역 sync(`await bot.tree.sync()`)로 되돌리면 된다.
    try:
        # 1) 전역으로 정의된 명령들을 각 길드에 복사해 즉시 등록한다.
        guild_count = 0
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            guild_count += 1

        if guild_count > 0:
            # 2) 기존 전역 등록본을 비워, 같은 명령이 (전역+길드) 중복으로 보이지 않게 한다.
            #    (이미 길드에 sync 됐으므로 전역을 비워도 명령은 사라지지 않는다.)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            COMMANDS_SYNCED = True
            print(
                f"Synced slash commands to {guild_count} guild(s) instantly. "
                f"Cleared global command copies."
            )
        else:
            # 길드 캐시가 비어 있는 예외 상황: 명령이 사라지지 않도록 전역으로라도 등록한다.
            synced = await bot.tree.sync()
            COMMANDS_SYNCED = True
            print(f"No guilds cached; fell back to global sync ({len(synced)} command(s)).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")
    
    print(f"------")

# --- Helper Business Logics ---
def normalize_display_name(display_name: str) -> str:
    normalized = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", display_name or "").strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized or display_name

async def perform_register(discord_user_id: str, mention_str: str, display_name: str, riot_id: str) -> tuple[bool, str]:
    if "#" not in riot_id:
        return False, "❌ **오류**: Riot 계정명은 `소환사명#태그` 형식으로 입력해야 합니다. (예: YaHoSmurf#KR2)"

    display_name = normalize_display_name(display_name)
    riot_name, riot_tag = riot_id.split("#", 1)

    async with httpx.AsyncClient(verify=False) as client:
        try:
            # 1. Look up existing Player record for this Discord User ID
            player_url = f"{BACKEND_URL}/api/players/discord/{discord_user_id}"
            player_resp = await client.get(player_url)
            
            player_id = None
            if player_resp.status_code == 200:
                player_data = player_resp.json()
                player_id = player_data["id"]
                # Update display name if it changed
                if player_data["display_name"] != display_name:
                    await client.put(f"{BACKEND_URL}/api/players/{player_id}", json={"display_name": display_name})
            elif player_resp.status_code == 404:
                create_payload = {
                    "display_name": display_name,
                    "discord_user_id": discord_user_id,
                }
                create_resp = await client.post(f"{BACKEND_URL}/api/players", json=create_payload)
                if create_resp.status_code == 201:
                    player_data = create_resp.json()
                    player_id = player_data["id"]
                else:
                    err_detail = create_resp.json().get("detail", "서버 내부 처리 오류")
                    return False, f"❌ **플레이어 생성 실패**: {err_detail}"
            else:
                return False, "❌ **백엔드 서버 API 응답 에러**가 발생했습니다. 백엔드를 확인해주세요."

            # 2. Call backend to link Riot Account
            link_payload = {
                "riot_game_name": riot_name.strip(),
                "riot_tag": riot_tag.strip(),
                "main_account": True
            }
            link_url = f"{BACKEND_URL}/api/players/{player_id}/accounts"
            link_resp = await client.post(link_url, json=link_payload, timeout=25.0)
            
            if link_resp.status_code == 200:
                return True, (
                    f"✅ **내전 플레이어 등록 완료**\n"
                    f"- 디스코드 유저: {mention_str}\n"
                    f"- 관리 닉네임: **{display_name}**\n"
                    f"- 롤 계정명: **{riot_name}#{riot_tag}** (DeepLOL PUUID 연동 완료)\n"
                    f"- 전적은 45분 주기 동기화 또는 `/동기화` 명령으로 수집됩니다."
                )
            else:
                err_detail = link_resp.json().get("detail", "해당 소환사를 DeepLOL에서 조회할 수 없습니다.")
                return False, (
                    f"❌ **롤 계정 연동 실패**\n"
                    f"이유: {err_detail}\n"
                    f"Riot ID 및 태그의 대소문자를 확인해주시고, 최근에 게임 플레이가 없는 계정인 경우 전적 검색 사이체(DeepLOL)에서 먼저 한 번 조회한 후 재시도해주세요."
                )

        except Exception as e:
            return False, f"❌ **API 연결 실패**: 백엔드 API 서버 오프라인 ({str(e)})"


async def perform_add_account(discord_user_id: str, riot_id: str, owner_label: str | None = None) -> tuple[bool, str]:
    if "#" not in riot_id:
        return False, "❌ **오류**: Riot 계정명은 `소환사명#태그` 형식으로 입력해야 합니다."

    riot_name, riot_tag = riot_id.split("#", 1)

    async with httpx.AsyncClient(verify=False) as client:
        try:
            # Look up existing Player
            player_resp = await client.get(f"{BACKEND_URL}/api/players/discord/{discord_user_id}")
            if player_resp.status_code != 200:
                target_text = f"대상 유저({owner_label})가" if owner_label else "먼저"
                return False, f"❌ {target_text} `/관리자 등록` 명령어를 사용하여 플레이어를 등록해야 합니다."
            
            player_data = player_resp.json()
            player_id = player_data["id"]
            player_name = player_data.get("display_name") or owner_label or discord_user_id
            
            link_payload = {
                "riot_game_name": riot_name.strip(),
                "riot_tag": riot_tag.strip(),
                "main_account": False
            }
            link_resp = await client.post(
                f"{BACKEND_URL}/api/players/{player_id}/accounts", 
                json=link_payload, 
                timeout=25.0
            )
            
            if link_resp.status_code == 200:
                return True, (
                    f"✅ **추가 계정 연동 완료**\n"
                    f"- 대상 플레이어: **{player_name}**"
                    f"{f' ({owner_label})' if owner_label and owner_label != player_name else ''}\n"
                    f"- 추가 롤 계정: **{riot_name}#{riot_tag}**\n"
                    f"- 전적은 45분 주기 동기화 또는 `/동기화` 명령으로 수집됩니다."
                )
            else:
                err_detail = link_resp.json().get("detail", "연동 실패")
                return False, f"❌ **계정 연동 실패**: {err_detail}"
        except Exception as e:
            return False, f"❌ **API 연결 실패**: {str(e)}"


def format_match_report_embed(report: dict) -> discord.Embed:
    """경기 리포트(팀 스탯 + 어워드 + 흐름)를 임베드 하나로 만든다.

    자동 포스팅과 /최근경기 가 같은 포맷을 쓴다.
    """
    winner = report.get("winner_team")
    winner_kr = "🔵 블루팀" if winner == "BLUE" else "🔴 레드팀"
    color = 0x63B3ED if winner == "BLUE" else 0xFC8181

    desc_parts = []
    if report.get("match_name"):
        desc_parts.append(f"🏷️ **{report['match_name']}**")
    meta_bits = []
    if report.get("duration_text"):
        meta_bits.append(f"⏱️ {report['duration_text']}")
    if report.get("game_end_epoch"):
        # 디스코드 상대 시각 표기: "26분 전"처럼 렌더링된다.
        meta_bits.append(f"🕐 종료 <t:{int(report['game_end_epoch'])}:R>")
    if meta_bits:
        desc_parts.append(" · ".join(meta_bits))
    flow = report.get("flow")
    if flow and flow.get("text"):
        desc_parts.append(f"📈 {flow['text']}")

    embed = discord.Embed(
        title=f"🏁 경기 리포트 — {winner_kr} 승리",
        description="\n".join(desc_parts),
        color=color,
    )

    # 라인별 이모지 — 팀 라인이 한눈에 정렬돼 보이도록 앞머리에 붙인다.
    position_emoji = {
        "TOP": "🗡️", "JUNGLE": "🌲", "MIDDLE": "🎯", "BOTTOM": "🏹", "UTILITY": "🩹",
    }

    def _team_lines(team: list[dict]) -> str:
        lines = []
        for v in team:
            name = f"**{v['name']}**" if v.get("registered") else str(v["name"])
            badge = " 👑" if v.get("mvp") else (" 🛡️" if v.get("ace") else "")
            role = position_emoji.get(v.get("position", ""), "▫️")
            lines.append(
                f"{role} **{v['champion']}** · {name}{badge}\n"
                f"┗ `{v['kda_text']}` · AI `{v['ai_score']}` · DPM `{v['dpm']}`"
            )
        return "\n".join(lines)

    teams = report.get("teams", {})
    blue_result = "🏆 승리" if winner == "BLUE" else "패배"
    red_result = "🏆 승리" if winner == "RED" else "패배"
    if teams.get("blue"):
        embed.add_field(name=f"🔵 블루팀 · {blue_result}", value=_team_lines(teams["blue"])[:1024], inline=False)
    if teams.get("red"):
        embed.add_field(name=f"🔴 레드팀 · {red_result}", value=_team_lines(teams["red"])[:1024], inline=False)

    awards = report.get("awards", [])
    if awards:
        award_lines = "\n".join(
            f"{a['emoji']} **{a['title']}** — {a['name']}"
            + (f" ({a['champion']})" if a.get("champion") else "")
            + f"\n┗ {a['detail']}"
            for a in awards
        )
        embed.add_field(name="🎖️ 어워드", value=award_lines[:1024], inline=False)

    # 이 경기에서 깨진 서버 기록 / 멀티킬 이벤트 — 경기 리포트의 하이라이트.
    new_records = report.get("new_records") or []
    if new_records:
        record_lines = "\n".join(
            f"{r['emoji']} **{r['label']}** — {r['name']}"
            + (f" ({r['champion']})" if r.get("champion") else "")
            + f"\n┗ {r['detail']}"
            for r in new_records[:5]
        )
        embed.add_field(name="🚨 서버 신기록!", value=record_lines[:1024], inline=False)

    # MVP 챔피언을 썸네일(우상단)로 얹어 리포트에 시각적 포인트를 준다.
    mvp_award = next((a for a in awards if a.get("title") == "MVP"), None)
    if mvp_award:
        mvp_thumb = champion_image_url(mvp_award.get("champion"))
        if mvp_thumb:
            embed.set_thumbnail(url=mvp_thumb)

    if report.get("match_id"):
        embed.set_footer(text=f"{report['match_id']} · 경기 상세는 대시보드에서")
    return embed


def format_weekly_report_embed(data: dict) -> discord.Embed:
    """주간 리포트 임베드 — 자동 발행과 /주간리포트 가 같은 포맷을 쓴다."""
    desc = (
        f"📅 **{data.get('period_label', '')}** · "
        f"🎮 {data.get('games', 0)}경기 · ⏱️ {data.get('hours', 0)}시간 · "
        f"👥 참여 {data.get('players', 0)}명"
    )
    embed = discord.Embed(title="📰 GOL 내전 주간 리포트", description=desc, color=0xF5C542)
    for section in data.get("sections", []):
        embed.add_field(
            name=f"{section['emoji']} {section['title']}",
            value="\n".join(section["lines"])[:1024],
            inline=False,
        )
    if data.get("mvp_champion"):
        thumb = champion_image_url(data["mvp_champion"])
        if thumb:
            embed.set_thumbnail(url=thumb)
    embed.set_footer(text="매주 월요일 저녁 8시 자동 발행 · 상세 기록은 대시보드에서")
    return embed


def format_hall_of_fame_embed(data: dict) -> discord.Embed:
    """명예의 전당 임베드 — 단일 경기 기록 + 누적 보드."""
    embed = discord.Embed(
        title="🏛️ GOL 명예의 전당",
        description=f"유효 경기 **{data.get('total_matches', 0)}판** 누적 기준",
        color=0xD4AF37,
    )
    records = data.get("records", [])
    if records:
        lines = [
            f"{r['emoji']} **{r['label']}** `{r['value_text']}`\n"
            f"┗ {r['name']} ({r['champion']}) · {r['date_text']}"
            for r in records
        ]
        embed.add_field(name="📜 단일 경기 최고 기록", value="\n".join(lines)[:1024], inline=False)
    for board in data.get("boards", []):
        embed.add_field(
            name=f"{board['emoji']} {board['title']}",
            value="\n".join(board["lines"])[:1024],
            inline=True,
        )
    embed.set_footer(text="기록이 깨지면 경기 리포트에서 🚨 로 알려드립니다")
    return embed


def format_analysis_embeds(
    target: discord.Member,
    player_name: str,
    report: dict,
) -> list[discord.Embed]:
    """플레이어 분석 API 응답을 디스코드 임베드 목록으로 변환한다.

    수치 · 포지션 평균 · 대비 %를 한 줄에 담는 정량 포맷을 기본으로 한다.
    """
    position_kr = {
        "TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드",
        "BOTTOM": "원딜", "UTILITY": "서폿", "UNKNOWN": "미정",
    }
    main_pos_label = report.get("main_position_label") or position_kr.get(
        report.get("main_position", "UNKNOWN"), "미정"
    )
    sub_pos = report.get("sub_position", "UNKNOWN")
    sub_pos_label = report.get("sub_position_label") or position_kr.get(sub_pos, "미정")
    if sub_pos and sub_pos != "UNKNOWN":
        pos_text = f"주 **{main_pos_label}** · 부 {sub_pos_label}"
    else:
        pos_text = f"**{main_pos_label}**"

    desc_lines = [
        f"📋 **{report.get('matches', 0)}판** 분석 · {pos_text} · 유형 **{report.get('player_type', '-')}**"
    ]
    if report.get("win_condition"):
        desc_lines.append(f"🎯 {report['win_condition']}")

    main_embed = discord.Embed(
        title=f"🔍 {player_name} 분석 리포트",
        description="\n".join(desc_lines),
        color=0x66FCF1,
    )
    main_embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    # 시그니처(가장 잘하는) 챔피언을 썸네일로 올려 리포트 첫 임베드에 시각적 포인트를 준다.
    signature_champ = (report.get("champion_analysis") or {}).get("best")
    if signature_champ:
        sig_thumb = champion_image_url(signature_champ.get("champion_name"))
        if sig_thumb:
            main_embed.set_thumbnail(url=sig_thumb)

    # 등급 → 방향 마커. 수치 크기 기준(높음=▲)이라 지표 좋고 나쁨과는 별개다.
    rating_arrows = {"높음": "▲", "보통": "─", "낮음": "▼"}

    summary_rows = report.get("summary_table", [])
    if summary_rows:
        summary_lines = []
        for row in summary_rows[:10]:
            arrow = rating_arrows.get(row.get("magnitude_rating") or row.get("relative_rating", "보통"), "─")
            base = row.get("formatted_position_avg")
            pct = row.get("pct_vs_avg")
            if base is not None and pct is not None:
                summary_lines.append(
                    f"`{arrow}` **{row['label']}** `{row['formatted_value']}` · 평균 {base} ({pct}%)"
                )
            else:
                summary_lines.append(
                    f"`{arrow}` **{row['label']}** `{row['formatted_value']}` — {row.get('relative_evaluation', '')}"
                )
        main_embed.add_field(
            name="📊 종합 지표 — 값 · 포지션 평균 (대비 %)",
            value="\n".join(summary_lines)[:1024],
            inline=False,
        )

    # 강점/약점은 좌우 2열(inline)로 붙여 스캔을 빠르게 한다.
    strengths = report.get("strengths_top3", [])
    if strengths:
        medals = ["🥇", "🥈", "🥉"]
        value = "\n".join(
            f"{medals[i] if i < len(medals) else '▫️'} **{it['label']}** `{it['value']}`\n└ 평균 {it['position_avg']}"
            for i, it in enumerate(strengths)
        )
        main_embed.add_field(name="💪 강점 TOP 3", value=value[:1024], inline=True)

    weaknesses = report.get("weaknesses_top3", [])
    if weaknesses:
        value = "\n".join(
            f"🔻 **{it['label']}** `{it['value']}`\n└ 평균 {it['position_avg']}"
            for it in weaknesses
        )
        main_embed.add_field(name="⚠️ 약점 TOP 3", value=value[:1024], inline=True)

    embeds = [main_embed]

    # ── ⚔️ 경기력 정량 분석 ─────────────────────────────────────────────
    perf_embed = discord.Embed(title="⚔️ 경기력 정량 분석", color=0x45A29E)

    lf = report.get("lane_final_analysis")
    if lf and lf.get("items"):
        lf_lines = "\n".join(
            f"{it.get('emoji', '')} **{it['label']}** `{it['value']}` · {it.get('rating', '보통')}\n└ {it['detail']}"
            for it in lf["items"]
        )
        perf_embed.add_field(
            name=f"라인전 → 후반 비교 ({lf.get('games', 0)}판)",
            value=lf_lines[:1024],
            inline=False,
        )

    # 포지션별 핵심 지표 — 값과 같은 포지션 평균을 나란히 표기.
    position_breakdown = report.get("position_breakdown", [])
    if position_breakdown:
        def _pos_brief(rows: list[dict], metric: str) -> str | None:
            for r in rows:
                if r.get("metric") == metric:
                    base = r.get("formatted_position_avg")
                    suffix = f" (평균 {base})" if base else ""
                    return f"{r['label']} `{r['formatted_value']}`{suffix}"
            return None

        pos_lines = []
        for pb in position_breakdown:
            if pb.get("locked"):
                pos_lines.append(
                    f"🔒 **{pb['position_label']}** {pb['games']}/{pb['min_games']}판 — 표본 부족"
                )
                continue
            rows = pb.get("summary_table", [])
            briefs = [b for b in (_pos_brief(rows, "win_rate"), _pos_brief(rows, "kda")) if b]
            pos_lines.append(
                f"📌 **{pb['position_label']}** {pb['games']}판\n└ " + " · ".join(briefs)
            )
        if pos_lines:
            perf_embed.add_field(
                name="🎯 포지션별 지표",
                value="\n".join(pos_lines)[:1024],
                inline=False,
            )

    # 성향 · 폼 — 태그 대신 수치를 앞세운 압축 라인.
    def _stars(n) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 0
        n = max(0, min(5, n))
        return "★" * n + "☆" * (5 - n)

    trait_lines = []
    rf = report.get("recent_form")
    if rf:
        last5 = "".join(rf.get("last5", []))
        trait_lines.append(
            f"{rf.get('emoji', '')} **최근 폼** `{last5}` · 최근 승률 {rf.get('recent_win_rate', 0)}% · {rf.get('tag', '')}"
        )
    pp = report.get("ping_profile")
    if pp:
        share = pp.get("dominant_share", 0)
        share_text = f" · 주 카테고리 비중 {share}%" if share else ""
        trait_lines.append(
            f"{pp.get('emoji', '')} **핑** `{pp.get('avg_total', 0)}회/판` · {pp.get('tag', '')}{share_text}"
        )
    lp = report.get("lane_performance")
    if lp:
        trait_lines.append(
            f"⚔️ **라인전/한타** AI점수 `{lp.get('lane_ai', 0)} → {lp.get('final_ai', 0)}` · "
            f"팀 내 `{lp.get('lane_rank', 0)}위 → {lp.get('final_rank', 0)}위` · "
            f"{lp.get('growth_emoji', '')} {lp.get('growth_tag', '')}"
        )
    mo = report.get("momentum")
    if mo:
        trait_lines.append(
            f"🎢 **승부 기질** 역전승 `{mo.get('comeback_wins', 0)}` · 리드 상실 `{mo.get('collapse_losses', 0)}` · "
            f"리드 유지 `{mo.get('lead_secured_wins', 0)}` ({mo.get('games_tracked', 0)}판 추적)"
        )
    if trait_lines:
        perf_embed.add_field(name="🧬 성향 · 폼", value="\n".join(trait_lines)[:1024], inline=False)

    if perf_embed.fields:
        embeds.append(perf_embed)

    # ── 📈 승패 요인 — 승리/패배 경기 평균을 지표별로 비교 ──────────────
    pattern_embed = discord.Embed(
        title="📈 승패 요인",
        description="승리 경기와 패배 경기의 지표 평균 비교 (Δ = 승리−패배)",
        color=0x1F2833,
    )
    detail = report.get("win_loss_detail", [])

    def _wl_fmt(item: dict, key: str) -> str:
        text = item.get(f"{key}_text")
        if text:
            return text
        return f"{item.get(f'{key}_value', 0):.2f}"

    def _wl_line(item: dict) -> str:
        delta = item.get("diff_text") or f"{item.get('diff', 0):+.2f}"
        return (
            f"• **{item['label']}** — 승 `{_wl_fmt(item, 'win')}` · 패 `{_wl_fmt(item, 'loss')}` (Δ {delta})"
        )

    if detail:
        positives = [d for d in detail if d.get("impact", 0) > 0][:3]
        negatives = [d for d in sorted(detail, key=lambda d: d.get("impact", 0)) if d.get("impact", 0) < 0][:3]
        if positives:
            pattern_embed.add_field(
                name="✅ 승리 경기에서 앞선 지표",
                value="\n".join(_wl_line(d) for d in positives)[:1024],
                inline=False,
            )
        if negatives:
            pattern_embed.add_field(
                name="❌ 승리로 연결되지 않은 지표",
                value="\n".join(_wl_line(d) for d in negatives)[:1024],
                inline=False,
            )
    if not pattern_embed.fields:
        # 구버전 백엔드 등으로 상세 비교가 없으면 문장형 패턴으로 대체.
        win_patterns = report.get("win_patterns", [])
        loss_patterns = report.get("loss_patterns", [])
        pattern_embed.add_field(
            name="✅ 승리 패턴",
            value="\n".join(f"• {line}" for line in win_patterns)[:1024] if win_patterns else "표본 부족",
            inline=False,
        )
        pattern_embed.add_field(
            name="❌ 패배 패턴",
            value="\n".join(f"• {line}" for line in loss_patterns)[:1024] if loss_patterns else "표본 부족",
            inline=False,
        )
    embeds.append(pattern_embed)

    champ = report.get("champion_analysis", {})

    def _champ_line(row: dict, marker: str) -> str:
        return (
            f"{marker} **{row['champion_name']}** — {row['games']}판 · "
            f"승률 `{row['win_rate']:.1f}%` · KDA `{row['kda']:.2f}` · "
            f"DPM `{row.get('dpm', 0):.0f}` · CS/분 `{row.get('cspm', 0):.1f}`"
        )

    highlight_lines = []
    if champ.get("best"):
        highlight_lines.append(_champ_line(champ["best"], "✅"))
    if champ.get("worst"):
        highlight_lines.append(_champ_line(champ["worst"], "❌"))
    table_lines = [_champ_line(row, "•") for row in champ.get("table", [])[:5]]

    if highlight_lines or table_lines:
        champ_embed = discord.Embed(title="⭐ 챔피언 분석", color=0x1F2833)
        if champ.get("best"):
            best_thumb = champion_image_url(champ["best"].get("champion_name"))
            if best_thumb:
                champ_embed.set_thumbnail(url=best_thumb)
        if highlight_lines:
            champ_embed.add_field(name="시그니처 · 고전", value="\n".join(highlight_lines)[:1024], inline=False)
        if table_lines:
            champ_embed.add_field(name="챔피언별 성적", value="\n".join(table_lines)[:1024], inline=False)
        embeds.append(champ_embed)

    # 🤝 관계 분석 (최고의 듀오 / 천적 등)
    synergy = report.get("synergy")
    if synergy:
        def _rel_line(item: dict | None, template: str) -> str | None:
            if not item:
                return None
            return template.format(
                name=item["name"], wr=item["win_rate"],
                w=item["wins"], l=item["losses"], g=item["games"],
            )

        rel_lines = [
            line for line in (
                _rel_line(synergy.get("best_duo"),
                          "🤝 **최고의 듀오** — {name}\n└ 같은 팀일 때 승률 `{wr}%` ({w}승 {l}패)"),
                _rel_line(synergy.get("worst_duo"),
                          "🧊 **안 맞는 듀오** — {name}\n└ 같은 팀일 때 승률 `{wr}%` ({w}승 {l}패)"),
                _rel_line(synergy.get("nemesis"),
                          "⚔️ **천적** — {name}\n└ 상대로 만나면 내 승률 `{wr}%` ({w}승 {l}패)"),
                _rel_line(synergy.get("favorite_prey"),
                          "😎 **밥줄** — {name}\n└ 상대로 만나면 내 승률 `{wr}%` ({w}승 {l}패)"),
            ) if line
        ]
        if rel_lines:
            min_games = synergy.get("min_games", 2)
            synergy_embed = discord.Embed(
                title="🤝 관계 분석",
                description=f"함께/상대로 {min_games}경기 이상 만난 플레이어 기준",
                color=0xC5C6C7,
            )
            synergy_embed.add_field(name="시너지 · 라이벌", value="\n".join(rel_lines)[:1024], inline=False)
            embeds.append(synergy_embed)

    return embeds


async def fetch_player_analysis(discord_user_id: str) -> tuple[bool, str | dict]:
    async with httpx.AsyncClient(verify=False) as client:
        try:
            player_resp = await client.get(f"{BACKEND_URL}/api/players/discord/{discord_user_id}")
            if player_resp.status_code == 404:
                return False, "❌ **분석 실패**: 해당 디스코드 유저는 등록된 플레이어가 아닙니다. `/관리자 등록`으로 먼저 등록해주세요."
            if player_resp.status_code != 200:
                return False, "❌ **백엔드 서버 API 응답 에러**가 발생했습니다. 백엔드를 확인해주세요."

            player_data = player_resp.json()
            player_id = player_data["id"]
            player_name = player_data.get("display_name") or discord_user_id

            analysis_resp = await client.get(
                f"{BACKEND_URL}/api/players/{player_id}/analysis",
                timeout=30.0,
            )
            if analysis_resp.status_code == 400:
                detail = analysis_resp.json().get("detail", "분석에 필요한 경기 데이터가 부족합니다.")
                return False, f"❌ **분석 불가**: {detail}"
            if analysis_resp.status_code == 404:
                return False, "❌ **분석 실패**: 플레이어를 찾을 수 없습니다."
            if analysis_resp.status_code != 200:
                return False, (
                    "❌ **분석 API 응답 에러**가 발생했습니다. 백엔드를 확인해주세요.\n"
                    f"`HTTP {analysis_resp.status_code}: {analysis_resp.text[:300]}`"
                )

            return True, {
                "player_name": player_name,
                "report": analysis_resp.json(),
            }
        except Exception as e:
            return False, f"❌ **API 연결 실패**: 백엔드 API 서버 오프라인 ({str(e)})"


async def perform_unregister(
    discord_user_id: str | None = None,
    owner_label: str | None = None,
    display_name: str | None = None,
) -> tuple[bool, str]:
    if not discord_user_id and not display_name:
        return False, "❌ **오류**: 등록취소할 디스코드명 또는 관리 닉네임을 입력해주세요."

    normalized_name = normalize_display_name(display_name) if display_name else None

    async with httpx.AsyncClient(verify=False) as client:
        try:
            players_to_delete = []
            if discord_user_id:
                player_resp = await client.get(f"{BACKEND_URL}/api/players/discord/{discord_user_id}")
                if player_resp.status_code == 200:
                    players_to_delete.append(player_resp.json())
                elif player_resp.status_code not in (404, 422):
                    return False, "❌ **백엔드 서버 API 응답 에러**가 발생했습니다. 백엔드를 확인해주세요."

            if normalized_name:
                players_resp = await client.get(f"{BACKEND_URL}/api/players", params={"limit": 1000})
                if players_resp.status_code != 200:
                    return False, "❌ **플레이어 목록 조회 실패**: 백엔드를 확인해주세요."
                for player in players_resp.json():
                    if normalize_display_name(player.get("display_name", "")) == normalized_name:
                        players_to_delete.append(player)

            unique_players = {player["id"]: player for player in players_to_delete}.values()
            if not unique_players:
                target = owner_label or normalized_name
                return False, f"❌ **등록취소 실패**: `{target}`에 해당하는 등록 플레이어를 찾지 못했습니다."

            deleted_names = []
            for player in unique_players:
                delete_resp = await client.delete(f"{BACKEND_URL}/api/players/{player['id']}")
                if delete_resp.status_code not in (200, 204):
                    return False, f"❌ **등록취소 실패**: `{player.get('display_name')}` 삭제 중 오류가 발생했습니다."
                deleted_names.append(player.get("display_name", str(player["id"])))

            return True, (
                "✅ **등록취소 완료**\n"
                f"- 삭제된 플레이어: **{', '.join(deleted_names)}**\n"
                "- 연결된 롤 계정도 함께 삭제되었습니다."
            )
        except Exception as e:
            return False, f"❌ **API 연결 실패**: {str(e)}"


# ── 관리자 명령 그룹 ──────────────────────────────────────────────────
# 명령 목록이 길어져 관리자 명령 4개(등록·계정추가·등록취소·공지채널)를 /관리자
# 하위 명령으로 묶는다. 디스코드 픽커는 가나다순 자동 정렬이라 표시 순서 지정은
# 불가능하지만, 그룹으로 묶으면 목록에서 한 항목으로 접힌다.
# default_permissions 는 의도적으로 걸지 않는다 — 화이트리스트(ADMIN_DISCORD_IDS)에
# 오른 비-서버관리자에게도 노출돼야 하며, 실행 차단은 각 명령의 reject_non_admin 이 담당.
admin_group = app_commands.Group(
    name="관리자", description="[관리자] 플레이어 등록·계정 연동·공지 채널 관리 명령 모음"
)
bot.tree.add_command(admin_group)


# --- Slash Command: /관리자 등록 ---
@admin_group.command(name="등록", description="[관리자] 플레이어를 등록하고 롤 계정을 연동합니다.")
@app_commands.rename(
    riot_id="롤계정명",
    managed_member="디스코드명",
    display_name="관리_닉네임",
)
@app_commands.describe(
    riot_id="롤 계정 (형식: 소환사명#태그, 예: YaHoSmurf#KR2)",
    managed_member="등록할 디스코드 서버 멤버입니다.",
    display_name="직접 입력할 관리 닉네임입니다. 예: 홍길동",
)
async def register_player(
    interaction: discord.Interaction,
    managed_member: discord.Member,
    display_name: str,
    riot_id: str,
):
    # 권한 확인을 defer 보다 먼저 수행한다. (먼저 defer 하면 비관리자에게도
    # 공개 "생각 중..." 표시가 남는다.)
    if await reject_non_admin(interaction):
        return
    await interaction.response.defer()

    async with presence("정보 불러오는중.."):
        success, message = await perform_register(
            str(managed_member.id), managed_member.mention, display_name, riot_id
        )
    await send_followup_autodelete(interaction, message)


# --- Slash Command: /팀생성 ---
@bot.tree.command(name="팀생성", description="참가자를 선택하여 팀 매칭 UI를 출력합니다.")
async def create_teams(interaction: discord.Interaction):
    voice_state = interaction.user.voice

    voice_members = []
    voice_channel_name = None
    if voice_state and voice_state.channel:
        voice_channel = voice_state.channel
        voice_members = [m for m in voice_channel.members if not m.bot]
        voice_channel_name = voice_channel.name

    # 등록 플레이어 전체 목록 — 셀렉트 옵션을 봇이 직접 채운다.
    # (디스코드 유저 검색 셀렉트는 클라이언트 캐시에 있는 멤버만 보여줘 일부가 누락된다.)
    registered_players: list[dict] = []
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{BACKEND_URL}/api/players", timeout=15)
            if resp.status_code == 200:
                registered_players = resp.json()
    except Exception:
        registered_players = []  # 백엔드 장애 시 View 가 유저 검색 셀렉트로 폴백

    view = MatchmakeView(voice_members, registered_players)

    if registered_players:
        registered_ids = {str(p.get("discord_user_id")) for p in registered_players}
        voice_registered = [m for m in voice_members if str(m.id) in registered_ids]
        voice_unregistered = [m for m in voice_members if str(m.id) not in registered_ids]
        lines = []
        if voice_channel_name:
            lines.append(
                f"🎙️ **{voice_channel_name}** 음성 채널의 등록 플레이어 "
                f"**{len(voice_registered)}명**을 미리 선택해두었습니다."
            )
            if voice_unregistered:
                names = ", ".join(m.display_name for m in voice_unregistered[:10])
                lines.append(
                    f"⚠️ 음성 채널의 미등록 멤버 {len(voice_unregistered)}명({names})은 "
                    f"목록에 없습니다. `/관리자 등록` 후 이용해주세요."
                )
        lines.append(
            "아래 목록(등록 플레이어 전원)에서 내전에 참가할 **10명**을 체크/해제로 맞춘 다음 "
            "**[팀 생성하기]** 버튼을 눌러주세요."
        )
        await send_response_autodelete(interaction, "\n".join(lines), view=view)
    elif voice_channel_name:
        await send_response_autodelete(
            interaction,
            f"🎙️ **{voice_channel_name}** 음성 채널에서 봇을 제외한 {len(voice_members)}명의 플레이어를 발견했습니다.\n"
            f"추가 플레이어가 필요하시다면 아래의 사용자 선택 메뉴에서 서버의 다른 멤버를 검색하여 추가하실 수 있습니다.\n"
            f"아래 메뉴에서 내전에 참가할 **10명**을 선택하신 다음 **[팀 생성하기]** 버튼을 눌러주세요.",
            view=view,
        )
    else:
        await send_response_autodelete(
            interaction,
            f"📢 음성 채널에 연결되어 있지 않습니다.\n"
            f"아래의 사용자 선택 메뉴에서 서버 전체 멤버를 검색하여 내전에 참가할 **10명**을 선택하신 다음 **[팀 생성하기]** 버튼을 눌러주세요.",
            view=view,
        )


# --- Slash Command: /동기화 ---
# [임시] 동기화 명령을 일반 유저도 사용할 수 있도록 권한 제한을 해제했다.
# 관리자 전용으로 되돌리려면 아래 default_permissions 데코레이터와
# reject_non_admin 체크를 다시 추가하면 된다.
@bot.tree.command(name="동기화", description="전체 전적 데이터를 새로 불러와 동기화합니다. (시간이 오래 걸릴 수 있음)")
async def trigger_inhouse_sync(interaction: discord.Interaction):
    await interaction.response.defer()

    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.post(f"{BACKEND_URL}/api/sync")
            if resp.status_code == 202:
                await send_followup_autodelete(
                    interaction,
                    "✅ **동기화 트리거 전송 완료**\n"
                    "전체 전적 데이터를 새로 불러오는 중이라 시간이 오래 걸릴 수 있습니다. "
                    "백엔드에서 최근 20경기를 갱신·조회하며 신규 커스텀 게임을 수집하는 중입니다. "
                    "완료 시 대시보드에서 전적을 확인하실 수 있습니다."
                )
            else:
                await send_followup_autodelete(interaction, "❌ **동기화 실패**: 백엔드가 요청을 접수하지 않았습니다.")
        except Exception as e:
            await send_followup_autodelete(interaction, f"❌ **연결 오류**: 백엔드가 응답하지 않습니다. ({str(e)})")


# --- Slash Command: /관리자 계정추가 ---
@admin_group.command(name="계정추가", description="[관리자] 기존 플레이어에 추가 롤 계정을 연동합니다.")
@app_commands.rename(
    riot_id="롤계정명",
    target="디스코드명",
)
@app_commands.describe(
    riot_id="추가할 롤 계정 (형식: 소환사명#태그, 예: 부캐닉#KR2)",
    target="계정을 추가할 등록 플레이어의 디스코드 서버 멤버입니다."
)
async def add_account(interaction: discord.Interaction, target: discord.Member, riot_id: str):
    if await reject_non_admin(interaction):
        return
    await interaction.response.defer()

    account_owner = target
    async with presence("정보 불러오는중.."):
        success, message = await perform_add_account(
            str(account_owner.id),
            riot_id,
            account_owner.mention
        )
    await send_followup_autodelete(interaction, message)


# --- Slash Command: /분석 ---
@bot.tree.command(name="분석", description="등록된 플레이어의 내전 성향 및 전적 분석 리포트를 조회합니다.")
@app_commands.rename(target="디스코드명")
@app_commands.describe(
    target="분석할 등록 플레이어의 디스코드 서버 멤버입니다.",
)
async def analyze_registered_player(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer()

    async with presence("정보 불러오는중.."):
        success, result = await fetch_player_analysis(str(target.id))
    if not success:
        await send_followup_autodelete(interaction, result)
        return

    embeds = format_analysis_embeds(target, result["player_name"], result["report"])
    await send_followup_autodelete(interaction, content=f"{target.mention} 플레이어 분석 결과입니다.", embeds=embeds)


# --- Slash Command: /등록취소 ---
@admin_group.command(name="등록취소", description="[관리자] 등록된 플레이어와 연결된 롤 계정을 모두 삭제합니다.")
@app_commands.rename(
    managed_member="디스코드명",
    display_name="관리_닉네임",
)
@app_commands.describe(
    managed_member="등록취소할 디스코드 서버 멤버입니다.",
    display_name="등록취소할 관리 닉네임입니다.",
)
async def unregister_player(
    interaction: discord.Interaction,
    managed_member: discord.Member | None = None,
    display_name: str | None = None,
):
    if await reject_non_admin(interaction):
        return
    await interaction.response.defer()

    success, message = await perform_unregister(
        str(managed_member.id) if managed_member else None,
        managed_member.mention if managed_member else None,
        display_name,
    )
    await send_followup_autodelete(interaction, message)


# --- Slash Command: /코드 ---
@bot.tree.command(name="코드", description="커스텀 게임 코드를 생성합니다.")
@app_commands.rename(match_name="매치이름")
@app_commands.describe(
    match_name="전적에 표시될 매치 이름 (기본값: 오늘날짜 GOL 내전)",
)
async def generate_code(interaction: discord.Interaction, match_name: str | None = None):
    await interaction.response.defer()

    # 이 채널을 경기 자동 리포트 채널로 기억한다 (코드 생성 성공 여부와 무관).
    # 이후 30분 이내에 끝난 경기가 색인되면 여기로 리포트가 올라온다.
    if interaction.channel_id:
        bot_state.set_announce_channel(
            interaction.channel_id,
            interaction.guild_id,
        )

    try:
        code = await generate_custom_code(match_name)
        from datetime import datetime
        used_name = match_name or f"{datetime.now().strftime('%Y-%m-%d')} GOL 내전"
        await send_followup_autodelete(
            interaction,
            f"🎮 **커스텀 게임 코드가 생성되었습니다!**\n"
            f"매치명: **{used_name}**\n"
            f"```\n{code}\n```\n"
            f"롤 클라이언트 → 커스텀 게임 → 코드로 참가 에서 위 코드를 입력하세요.\n"
            f"📌 경기가 끝나면 이 채널에 분석 리포트가 자동으로 올라옵니다.",
        )
    except Exception as e:
        await send_followup_autodelete(
            interaction,
            f"❌ **커스텀 코드 생성 실패:** {e}\n잠시 후 다시 시도해주세요.",
        )


# --- Slash Command: /최근경기 ---
@bot.tree.command(name="최근경기", description="가장 최근에 끝난 내전 경기의 분석 리포트를 보여줍니다.")
async def recent_match(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/matches/reports",
                params={"limit": 1},
                timeout=20,
            )
        reports = resp.json().get("reports", []) if resp.status_code == 200 else []
    except Exception:
        await send_followup_autodelete(
            interaction,
            "❌ **백엔드 서버에 연결할 수 없습니다.** 잠시 후 다시 시도해주세요.",
        )
        return

    if not reports:
        await send_followup_autodelete(
            interaction,
            "📭 아직 저장된 내전 경기가 없습니다. 경기 후 `/동기화`를 실행해보세요.",
        )
        return

    # 경기 리포트는 채널에 남는 콘텐츠라 자동삭제하지 않는다.
    file = await fetch_mvp_card_file(reports[0]["match_id"])
    if file:
        await interaction.followup.send(embed=format_match_report_embed(reports[0]), file=file)
    else:
        await interaction.followup.send(embed=format_match_report_embed(reports[0]))


# --- Slash Command: /주간리포트 ---
@bot.tree.command(name="주간리포트", description="지난 7일 내전 결산 리포트를 보여줍니다. (매주 월요일 저녁 자동 발행)")
async def weekly_report_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{BACKEND_URL}/api/reports/weekly", params={"days": 7}, timeout=30)
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        await send_followup_autodelete(
            interaction, "❌ **백엔드 서버에 연결할 수 없습니다.** 잠시 후 다시 시도해주세요."
        )
        return

    if not data.get("games"):
        await send_followup_autodelete(interaction, "📭 지난 7일 동안 기록된 내전 경기가 없습니다.")
        return

    # 주간 리포트는 채널에 남는 콘텐츠라 자동삭제하지 않는다.
    await interaction.followup.send(embed=format_weekly_report_embed(data))


# --- Slash Command: /명예의전당 ---
@bot.tree.command(name="명예의전당", description="서버 통산 기록 보유자와 누적 MVP·다승 랭킹을 보여줍니다.")
async def hall_of_fame_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{BACKEND_URL}/api/hall-of-fame", timeout=30)
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        await send_followup_autodelete(
            interaction, "❌ **백엔드 서버에 연결할 수 없습니다.** 잠시 후 다시 시도해주세요."
        )
        return

    if not data.get("records") and not data.get("boards"):
        await send_followup_autodelete(interaction, "📭 아직 기록이 쌓이지 않았습니다. 내전을 더 진행해보세요!")
        return

    # 카드 이미지를 우선 시도하고, 생성 실패 시 기존 임베드로 폴백한다.
    # 명예의 전당은 채널에 남는 콘텐츠라 자동삭제하지 않는다.
    card_resp = None
    try:
        async with httpx.AsyncClient(verify=False) as client:
            card_resp = await client.get(f"{BACKEND_URL}/api/hall-of-fame/card", timeout=60.0)
    except Exception:
        pass
    if card_resp is not None and card_resp.status_code == 200:
        file = discord.File(io.BytesIO(card_resp.content), filename="gol_hall_of_fame.png")
        await interaction.followup.send(file=file)
        return
    await interaction.followup.send(embed=format_hall_of_fame_embed(data))


# --- Slash Command: /스타일카드 ---
@bot.tree.command(name="스타일카드", description="내 플레이 스타일과 지표가 담긴 공유용 카드 이미지를 만들어줍니다.")
@app_commands.rename(target="디스코드명")
@app_commands.describe(target="카드를 만들 등록 플레이어의 디스코드 서버 멤버입니다. (생략 시 본인)")
async def style_card_command(interaction: discord.Interaction, target: discord.Member | None = None):
    member = target or interaction.user
    await interaction.response.defer()

    async with presence("카드 만드는중.."):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                player_resp = await client.get(f"{BACKEND_URL}/api/players/discord/{member.id}")
                if player_resp.status_code == 404:
                    await send_followup_autodelete(
                        interaction,
                        f"❌ {member.mention} 은(는) 등록된 플레이어가 아닙니다. `/관리자 등록`으로 먼저 등록해주세요.",
                    )
                    return
                if player_resp.status_code != 200:
                    await send_followup_autodelete(interaction, "❌ **백엔드 서버 API 응답 에러**가 발생했습니다.")
                    return
                player = player_resp.json()

                card_resp = await client.get(
                    f"{BACKEND_URL}/api/players/{player['id']}/style-card",
                    timeout=60.0,  # 첫 호출은 스플래시 아트 다운로드로 수 초 걸릴 수 있다.
                )
        except Exception as e:
            await send_followup_autodelete(interaction, f"❌ **API 연결 실패**: {str(e)}")
            return

    if card_resp.status_code == 400:
        detail = card_resp.json().get("detail", "분석에 필요한 경기 데이터가 부족합니다.")
        await send_followup_autodelete(interaction, f"❌ **카드 생성 불가**: {detail}")
        return
    if card_resp.status_code != 200:
        await send_followup_autodelete(
            interaction, f"❌ **카드 생성 실패**: 백엔드를 확인해주세요. (HTTP {card_resp.status_code})"
        )
        return

    file = discord.File(io.BytesIO(card_resp.content), filename="gol_style_card.png")
    # 카드는 공유/자랑용 콘텐츠라 자동삭제하지 않는다.
    await interaction.followup.send(content=f"🃏 {member.mention} 님의 플레이스타일 카드입니다!", file=file)


# --- Slash Command: /파워랭킹 ---
@bot.tree.command(name="파워랭킹", description="내전 Elo MMR 기준 서버 랭킹 TOP 10 카드를 보여줍니다.")
async def power_ranking_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with presence("랭킹 집계중.."):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(f"{BACKEND_URL}/api/power-ranking/card", timeout=60.0)
        except Exception as e:
            await send_followup_autodelete(interaction, f"❌ **API 연결 실패**: {str(e)}")
            return

    if resp.status_code == 404:
        await send_followup_autodelete(
            interaction, "📭 랭킹에 올릴 플레이어가 아직 없습니다. 내전을 더 진행해보세요!"
        )
        return
    if resp.status_code != 200:
        await send_followup_autodelete(
            interaction, f"❌ **카드 생성 실패**: 백엔드를 확인해주세요. (HTTP {resp.status_code})"
        )
        return

    file = discord.File(io.BytesIO(resp.content), filename="gol_power_ranking.png")
    # 랭킹 카드는 채널에 남는 콘텐츠라 자동삭제하지 않는다.
    await interaction.followup.send(content="⚡ 이번 주 GOL 파워랭킹입니다!", file=file)


# --- Slash Command: /경기흐름 ---
@bot.tree.command(name="경기흐름", description="가장 최근 내전 경기의 승률 흐름 그래프 카드를 만들어줍니다.")
async def match_flow_command(interaction: discord.Interaction):
    await interaction.response.defer()
    async with presence("그래프 그리는중.."):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(
                    f"{BACKEND_URL}/api/matches/latest/momentum-card",
                    timeout=60.0,  # 첫 호출은 이미지 캐시 준비로 수 초 걸릴 수 있다.
                )
        except Exception as e:
            await send_followup_autodelete(interaction, f"❌ **API 연결 실패**: {str(e)}")
            return

    if resp.status_code == 404:
        await send_followup_autodelete(
            interaction, "📭 흐름 그래프를 그릴 수 있는 경기가 아직 없습니다. 경기 후 `/동기화`를 실행해보세요."
        )
        return
    if resp.status_code != 200:
        await send_followup_autodelete(
            interaction, f"❌ **카드 생성 실패**: 백엔드를 확인해주세요. (HTTP {resp.status_code})"
        )
        return

    file = discord.File(io.BytesIO(resp.content), filename="gol_match_flow.png")
    # 경기 흐름 카드는 채널에 남는 콘텐츠라 자동삭제하지 않는다.
    await interaction.followup.send(content="📈 가장 최근 내전 경기의 흐름입니다!", file=file)


# --- Slash Command: /듀오카드 ---
@bot.tree.command(name="듀오카드", description="두 플레이어가 같은 팀일 때의 성적을 담은 듀오 시너지 카드를 만들어줍니다.")
@app_commands.rename(partner="듀오상대", target="기준플레이어")
@app_commands.describe(
    partner="듀오 시너지를 확인할 상대 멤버입니다.",
    target="기준이 될 멤버입니다. (생략 시 본인)",
)
async def duo_card_command(
    interaction: discord.Interaction,
    partner: discord.Member,
    target: discord.Member | None = None,
):
    member_a = target or interaction.user
    member_b = partner
    await interaction.response.defer()

    if member_a.id == member_b.id:
        await send_followup_autodelete(interaction, "❌ 서로 다른 두 명을 지정해주세요.")
        return

    async with presence("카드 만드는중.."):
        try:
            async with httpx.AsyncClient(verify=False) as client:
                ids = []
                for member in (member_a, member_b):
                    player_resp = await client.get(f"{BACKEND_URL}/api/players/discord/{member.id}")
                    if player_resp.status_code == 404:
                        await send_followup_autodelete(
                            interaction,
                            f"❌ {member.mention} 은(는) 등록된 플레이어가 아닙니다. `/관리자 등록`으로 먼저 등록해주세요.",
                        )
                        return
                    if player_resp.status_code != 200:
                        await send_followup_autodelete(interaction, "❌ **백엔드 서버 API 응답 에러**가 발생했습니다.")
                        return
                    ids.append(player_resp.json()["id"])

                card_resp = await client.get(
                    f"{BACKEND_URL}/api/duo-card",
                    params={"player_a": ids[0], "player_b": ids[1]},
                    timeout=60.0,  # 첫 호출은 스플래시 아트 다운로드로 수 초 걸릴 수 있다.
                )
        except Exception as e:
            await send_followup_autodelete(interaction, f"❌ **API 연결 실패**: {str(e)}")
            return

    if card_resp.status_code == 400:
        detail = card_resp.json().get("detail", "카드에 필요한 경기 데이터가 부족합니다.")
        await send_followup_autodelete(interaction, f"❌ **카드 생성 불가**: {detail}")
        return
    if card_resp.status_code != 200:
        await send_followup_autodelete(
            interaction, f"❌ **카드 생성 실패**: 백엔드를 확인해주세요. (HTTP {card_resp.status_code})"
        )
        return

    file = discord.File(io.BytesIO(card_resp.content), filename="gol_duo_card.png")
    # 듀오 카드는 공유/자랑용 콘텐츠라 자동삭제하지 않는다.
    await interaction.followup.send(
        content=f"🤝 {member_a.mention} ✕ {member_b.mention} 듀오 시너지 카드입니다!", file=file
    )


# --- Slash Command: /관리자 공지채널 ---
@admin_group.command(name="공지채널", description="[관리자] 경기 리포트·주간 리포트가 올라올 채널을 이 채널로 설정합니다.")
async def set_announce_channel_command(interaction: discord.Interaction):
    if await reject_non_admin(interaction):
        return
    if not interaction.channel_id:
        await send_response_autodelete(interaction, "❌ 이 위치에서는 공지 채널을 설정할 수 없습니다.", ephemeral=True)
        return
    bot_state.set_announce_channel(interaction.channel_id, interaction.guild_id)
    await send_response_autodelete(
        interaction,
        "✅ 이 채널을 공지 채널로 설정했습니다.\n"
        "- 경기 종료 후 자동 리포트\n- 매주 월요일 저녁 8시 주간 리포트\n"
        "가 이 채널에 올라옵니다. (`/코드` 사용 시에도 해당 채널로 갱신됩니다)",
        ephemeral=True,
    )


# --- Slash Command: /청소 ---
# 자동삭제(5분)는 메모리상의 asyncio 타이머라, 메시지를 보낸 뒤 5분이 지나기 전에
# 봇을 재시작하면 타이머가 사라져 그 메시지는 채널에 영구히 남는다.
# 이 명령은 그렇게 남은(또는 아직 안 지워진) 봇 메시지를 채널에서 한 번에 정리한다.
@bot.tree.command(name="청소", description="봇이 이 채널에 남긴 메시지를 한 번에 모두 삭제합니다.")
@app_commands.rename(scan_limit="검색범위")
@app_commands.describe(
    scan_limit="최근 몇 개의 메시지를 훑어 봇 메시지를 지울지 (기본 500, 최대 2000)",
)
async def clear_bot_messages(interaction: discord.Interaction, scan_limit: int = 500):
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        await send_response_autodelete(
            interaction,
            "❌ 이 채널에서는 메시지를 정리할 수 없습니다.",
            ephemeral=True,
        )
        return

    scan_limit = max(1, min(scan_limit, 2000))
    # 진행 안내는 ephemeral(본인만 보임)로 띄워, 정리 대상에 스스로 끼지 않게 한다.
    await interaction.response.defer(ephemeral=True, thinking=True)

    def _is_bot_message(message: discord.Message) -> bool:
        return bot.user is not None and message.author.id == bot.user.id

    deleted = 0
    try:
        # 14일 이내 메시지는 일괄 삭제, 그보다 오래된 것은 개별 삭제로 discord.py가 자동 폴백한다.
        purged = await channel.purge(limit=scan_limit, check=_is_bot_message, bulk=True)
        deleted = len(purged)
    except discord.Forbidden:
        # '메시지 관리' 권한이 없어 일괄 삭제가 막히면, 봇이 자기 메시지를 하나씩 지운다.
        # (자기 메시지 삭제는 별도 권한이 필요 없다.)
        async for message in channel.history(limit=scan_limit):
            if _is_bot_message(message):
                try:
                    await message.delete()
                    deleted += 1
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    await send_followup_autodelete(
        interaction,
        f"🧹 이 채널에서 봇 메시지 **{deleted}개**를 정리했습니다. (최근 {scan_limit}개 검사)",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("WARNING: DISCORD_BOT_TOKEN is not configured in .env. Bot will not run.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
