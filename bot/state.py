"""봇 런타임 상태 영속화 — 재시작해도 유지되어야 하는 값들.

- 경기 자동 리포트를 올릴 채널(/코드 를 마지막으로 사용한 채널)
- 이미 공지한 매치 id 목록(중복 포스팅 방지)

메모리상 타이머/변수는 봇 재시작 시 사라지므로 JSON 파일로 보관한다.
"""

from __future__ import annotations

import json
import os
import threading

_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
STATE_PATH = os.path.join(_STATE_DIR, "bot_state.json")

_lock = threading.Lock()
# 공지 이력은 최근 것만 있으면 충분하다(30분 창 + 여유).
_MAX_ANNOUNCED = 200


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_PATH)


def get_announce_channel_id() -> int | None:
    value = _load().get("announce_channel_id")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def set_announce_channel(channel_id: int, guild_id: int | None = None) -> None:
    with _lock:
        data = _load()
        data["announce_channel_id"] = int(channel_id)
        if guild_id is not None:
            data["announce_guild_id"] = int(guild_id)
        _save(data)


def get_last_weekly_report_week() -> str | None:
    """마지막으로 주간 리포트를 발행한 ISO 주차 키(예: '2026-W28')."""
    value = _load().get("last_weekly_report_week")
    return str(value) if value else None


def set_last_weekly_report_week(week_key: str) -> None:
    with _lock:
        data = _load()
        data["last_weekly_report_week"] = str(week_key)
        _save(data)


def is_announced(match_id: str) -> bool:
    return str(match_id) in _load().get("announced_match_ids", [])


def mark_announced(match_id: str) -> None:
    with _lock:
        data = _load()
        announced = data.get("announced_match_ids", [])
        if str(match_id) not in announced:
            announced.append(str(match_id))
        data["announced_match_ids"] = announced[-_MAX_ANNOUNCED:]
        _save(data)
