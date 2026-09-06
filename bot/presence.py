"""봇 상태(Presence / "듣는 중") 관리.

Discord 상태 메시지는 봇 전역에 하나만 표시된다. 여러 명령이 동시에 돌 수 있고
백엔드 동기화도 겹칠 수 있으므로, 활성 상태들을 모아 우선순위가 가장 높은 것만 보여주고
아무것도 없으면 기본 상태로 돌아간다. (레이트리밋 회피 위해 "바뀔 때만" 갱신)

main.py 와 views/* 가 순환 import 없이 공유할 수 있도록 별도 모듈로 분리했다.
"""
import asyncio
import contextlib
import itertools

import discord

from bot.config import BOT_DEFAULT_STATUS

DEFAULT_STATUS = BOT_DEFAULT_STATUS
PRIORITY_COMMAND = 10
PRIORITY_SYNC = 20  # 동기화 상태가 명령어 처리 상태보다 우선 표시된다.

_client: discord.Client | None = None
_holds: dict[int, tuple[int, str]] = {}  # token -> (priority, text)
_seq = itertools.count()
_lock = asyncio.Lock()


def set_client(client: discord.Client):
    """봇 준비 후 한 번 호출해 change_presence 대상 클라이언트를 등록한다."""
    global _client
    _client = client


async def _refresh():
    if _client is None:
        return
    if _holds:
        _, text = max(_holds.values(), key=lambda pt: pt[0])
    else:
        text = DEFAULT_STATUS
    try:
        await _client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=text)
        )
    except Exception as e:
        print(f"presence update failed: {e}")


async def refresh():
    """현재 활성 상태를 다시 적용한다(기본 상태 표시에도 사용)."""
    async with _lock:
        await _refresh()


async def acquire(text: str, priority: int = PRIORITY_COMMAND) -> int:
    """상태 하나를 켜고 토큰을 돌려준다. release(token) 으로 내려야 한다.

    context manager 로 감싸기 어려운 폴링 루프 같은 곳에서 쓴다."""
    token = next(_seq)
    async with _lock:
        _holds[token] = (priority, text)
        await _refresh()
    return token


async def release(token: int):
    async with _lock:
        _holds.pop(token, None)
        await _refresh()


@contextlib.asynccontextmanager
async def presence(text: str, priority: int = PRIORITY_COMMAND):
    """`async with presence('정보 불러오는중..'):` 로 감싸면 종료 시 자동 복귀한다."""
    token = await acquire(text, priority)
    try:
        yield
    finally:
        await release(token)
