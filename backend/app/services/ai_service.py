import httpx
from typing import Dict, Any
from app.config import settings


class AIService:
    def __init__(self):
        self.base_url = settings.OLLAMA_BASE_URL.rstrip("/")
        self.model = settings.OLLAMA_MODEL
        self.timeout = settings.OLLAMA_TIMEOUT_SECONDS

    def _build_team_context(self, matchmaking_result: Dict[str, Any]) -> str:
        blue_team = matchmaking_result.get("blue_team", {})
        red_team = matchmaking_result.get("red_team", {})
        mmr_diff = matchmaking_result.get("mmr_difference", 0.0)

        if hasattr(blue_team, "model_dump"):
            blue_team = blue_team.model_dump()
        if hasattr(red_team, "model_dump"):
            red_team = red_team.model_dump()

        blue_players_list = blue_team.get("players", [])
        red_players_list = red_team.get("players", [])

        blue_players_str = ", ".join(
            [f"{p.get('display_name', p.display_name if hasattr(p, 'display_name') else '?')}(MMR: {p.get('mmr', getattr(p, 'mmr', '?'))})" for p in blue_players_list]
        )
        red_players_str = ", ".join(
            [f"{p.get('display_name', p.display_name if hasattr(p, 'display_name') else '?')}(MMR: {p.get('mmr', getattr(p, 'mmr', '?'))})" for p in red_players_list]
        )

        return f"""
[블루팀]
- 플레이어: {blue_players_str}
- 평균 MMR: {blue_team.get('avg_mmr')}

[레드팀]
- 플레이어: {red_players_str}
- 평균 MMR: {red_team.get('avg_mmr')}

[매치 정보]
- 팀 평균 MMR 차이: {mmr_diff:.1f}
"""

    async def _call_ollama(self, prompt: str, timeout: float | None = None) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        url = f"{self.base_url}/api/generate"
        request_timeout = timeout if timeout is not None else self.timeout
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            try:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    return response.json().get("response", "코멘터리를 생성할 수 없습니다.")
                return f"🤖 AI 분석 보조: Ollama 서버 응답 에러 (HTTP {response.status_code})."
            except httpx.TimeoutException:
                return (
                    f"🤖 AI 분석 보조: Ollama 응답 시간 초과 ({int(request_timeout)}초). "
                    f"모델 '{self.model}' 첫 실행 시 로딩에 1분 이상 걸릴 수 있습니다. "
                    "터미널에서 `ollama run qwen3:8b`로 한 번 워밍업한 뒤 다시 시도해주세요."
                )
            except httpx.ConnectError:
                return (
                    f"🤖 AI 분석 보조: Ollama 서버에 연결할 수 없습니다 ({self.base_url}). "
                    "Ollama 앱이 실행 중인지, 또는 `ollama serve`가 켜져 있는지 확인해주세요."
                )
            except Exception as e:
                return f"🤖 AI 분석 보조: Ollama 호출 오류 ({type(e).__name__}: {e})"

    async def generate_match_commentary(self, matchmaking_result: Dict[str, Any]) -> str:
        """매치업 이후 별도로 호출되는 AI 경기 예측 해설."""
        context = self._build_team_context(matchmaking_result)
        prompt = f"""
리그 오브 레전드 사설 게임(내전) 매치업 정보가 다음과 같이 생성되었습니다.
이 매치에 대한 세밀한 전력 분석과 경기 결과 예측 코멘터리를 재미있고 흥미진진하게 2-3문단 분량의 한국어로 작성해주세요.

{context}

분석 가이드라인:
1. 각 팀에서 MMR이 가장 높은 에이스 플레이어를 짚어주고 그들이 게임 흐름을 어떻게 바꿀지 설명하세요.
2. 평균 MMR 차이가 큰가 작은가에 따라 경기가 박빙이 될지 원사이드가 될지 언급하세요.
3. 실제 e스포츠 공식 중계진(캐스터/해설)처럼 흥미롭고 에너제틱한 톤앤매너로 작성하세요.
"""
        return await self._call_ollama(prompt)

    async def generate_match_preview(self, matchmaking_result: Dict[str, Any]) -> str:
        """하위 호환용 — generate_match_commentary와 동일."""
        return await self.generate_match_commentary(matchmaking_result)


ai_service = AIService()
