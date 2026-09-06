"""
DeepLOL Clash API 연동 모듈.
로그인 후 커스텀 게임 토너먼트 코드를 생성한다.
"""

from datetime import datetime
import httpx

from bot.config import CLASH_PASSWORD

CLASH_API_BASE = "https://b2c-api.deeplol.gg"
CLASH_HEADERS = {
    "Origin": "https://clash.deeplol.gg",
    "Referer": "https://clash.deeplol.gg/",
}


async def _login() -> int:
    """DeepLOL Clash에 로그인하고 server_id를 반환한다."""
    if not CLASH_PASSWORD:
        raise ValueError(
            "CLASH_PASSWORD 가 설정되지 않았습니다. .env 파일에 CLASH_PASSWORD 를 추가해주세요."
        )
    async with httpx.AsyncClient(headers=CLASH_HEADERS, timeout=10) as client:
        resp = await client.post(
            f"{CLASH_API_BASE}/tournament/tournament_login",
            params={"password": CLASH_PASSWORD},
            json={"password": CLASH_PASSWORD},
        )
        resp.raise_for_status()
        data = resp.json()
        server_id = data.get("server_id")
        if not server_id:
            raise ValueError(f"로그인 실패: {data}")
        return int(server_id)


async def generate_custom_code(match_name: str | None = None) -> str:
    """
    커스텀 게임 토너먼트 코드를 생성해 반환한다.
    match_name 미지정 시 '오늘날짜 GOL 내전' 형식을 사용한다.
    """
    if not match_name:
        today = datetime.now().strftime("%Y-%m-%d")
        match_name = f"{today} GOL 내전"

    server_id = await _login()

    async with httpx.AsyncClient(headers=CLASH_HEADERS, timeout=10) as client:
        resp = await client.get(
            f"{CLASH_API_BASE}/tournament/tournament_code",
            params={
                "server_id": str(server_id),
                "server_name": match_name,
                "server_comment": "",
                "spectator_type": "LOBBYONLY",
                "pick_type": "TOURNAMENT_DRAFT",
                "map_name": "SUMMONERS_RIFT",
                "platform_id": "KR",
            },
        )
        resp.raise_for_status()
        codes: list[str] = resp.json().get("tournament_code_list", [])
        if not codes:
            raise ValueError("코드 생성 응답에 코드가 없습니다.")
        return codes[0]
