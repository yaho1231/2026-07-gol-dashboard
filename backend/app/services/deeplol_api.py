import httpx
import urllib.parse
from typing import Optional, Dict, Any


def parse_solo_rank_from_realtime(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Parse current solo ranked info from summoner-realtime API response."""
    result: Dict[str, Any] = {
        "solo_tier": None,
        "solo_division": None,
        "solo_league_points": None,
        "solo_high_tier": None,
    }
    if not data:
        return result

    solo = data.get("season_tier_info_dict", {}).get("ranked_solo_5x5", {})
    if not solo:
        return result

    tier = solo.get("tier")
    if tier:
        result["solo_tier"] = str(tier).upper()

    division = solo.get("division")
    if division is not None:
        result["solo_division"] = int(division)

    league_points = solo.get("league_points")
    if league_points is not None:
        result["solo_league_points"] = int(league_points)

    high_tier = solo.get("high_tier")
    if high_tier:
        result["solo_high_tier"] = str(high_tier)

    return result


class DeepLOLAPIClient:
    BASE_URL = "https://b2c-api-cdn.deeplol.gg"
    # 전적 갱신(re-crawl) 요청은 별도 renew 도메인으로 보낸다.
    RENEW_URL = "https://renew.deeplol.gg"

    def __init__(self):
        # Using headers to emulate standard browser behavior and bypass simple blocking.
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": "https://www.deeplol.gg",
            "Referer": "https://www.deeplol.gg/"
        }

    async def get_summoner_info(self, name: str, tag: str) -> Optional[Dict[str, Any]]:
        """
        Fetches summoner details to extract their unique puu_id.
        """
        encoded_name = urllib.parse.quote(name)
        encoded_tag = urllib.parse.quote(tag)
        url = f"{self.BASE_URL}/summoner/summoner?riot_id_name={encoded_name}&riot_id_tag_line={encoded_tag}&platform_id=KR"
        
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"Error fetching summoner {name}#{tag}: Status {response.status_code}")
                    return None
            except Exception as e:
                print(f"Exception fetching summoner {name}#{tag}: {e}")
                return None

    async def get_summoner_realtime(
        self,
        puu_id: str,
        summoner_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches current-season ranked tier info for a summoner.
        """
        encoded_puu_id = urllib.parse.quote(puu_id)
        encoded_summoner_id = urllib.parse.quote(summoner_id or "")
        url = (
            f"{self.BASE_URL}/summoner/summoner-realtime"
            f"?platform_id=KR&summoner_id={encoded_summoner_id}&puu_id={encoded_puu_id}"
        )

        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
                print(f"Error fetching realtime tier for {puu_id}: Status {response.status_code}")
                return None
            except Exception as e:
                print(f"Exception fetching realtime tier for {puu_id}: {e}")
                return None

    async def refresh_matches(
        self,
        puu_id: str,
        platform_id: str = "KR",
        queue_type: str = "ALL",
        start_idx: int = 0,
        count: int = 20,
    ) -> bool:
        """
        deeplol 서버에 해당 소환사의 최근 경기를 새로 크롤링하도록 요청한다(전적 갱신).

        match list 를 조회하기 전에 호출하면, 아직 deeplol 에 색인되지 않은 최신 경기까지
        포함되어 내려온다. POST 가 201(Created)/200 이면 갱신 작업이 접수된 것이며,
        실제 반영까지는 약간의 지연이 있을 수 있다(호출부에서 잠시 대기 후 조회).
        """
        url = f"{self.RENEW_URL}/match/refresh-matches"
        payload = {
            "puu_id": puu_id,
            "platform_id": platform_id,
            "queue_type": queue_type,
            "start_idx": start_idx,
            "count": count,
        }
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                response = await client.post(url, json=payload)
                if response.status_code in (200, 201):
                    return True
                print(f"Error refreshing matches for {puu_id}: Status {response.status_code}")
                return False
            except Exception as e:
                print(f"Exception refreshing matches for {puu_id}: {e}")
                return False

    async def get_match_list(
        self, puu_id: str, offset: int = 0, count: int = 20, only_list: int = 1
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches recent matches for a given summoner puu_id.

        only_list=1 -> 가벼운 응답: `match_id_list`(match_id, 생성시각)만 채워진다.
        only_list=0 -> 무거운 응답: `match_json_list`에 각 경기의 전체 데이터
                       (`match_basic_dict` + `participants_list`, queue_id 포함)가 함께 온다.
                       이 데이터는 get_match_details() 응답과 동일하므로, 이를 쓰면
                       경기별 상세 조회를 생략하고 한 번의 호출로 내전 여부까지 판별할 수 있다.
        """
        url = f"{self.BASE_URL}/match/matches?puu_id={puu_id}&platform_id=KR&offset={offset}&count={count}&queue_type=ALL&champion_id=0&only_list={only_list}&last_updated_at=0"
        async with httpx.AsyncClient(headers=self.headers, timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"Error fetching match list for {puu_id}: Status {response.status_code}")
                    return None
            except Exception as e:
                print(f"Exception fetching match list for {puu_id}: {e}")
                return None

    async def get_match_details(self, match_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetches detailed statistics for a specific match ID.
        """
        url = f"{self.BASE_URL}/match/match-cached?match_id={match_id}&platform_id=KR"
        async with httpx.AsyncClient(headers=self.headers, timeout=15.0) as client:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.json()
                else:
                    print(f"Error fetching match details for {match_id}: Status {response.status_code}")
                    return None
            except Exception as e:
                print(f"Exception fetching match details for {match_id}: {e}")
                return None

deeplol_client = DeepLOLAPIClient()
