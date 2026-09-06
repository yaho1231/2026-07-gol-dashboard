from datetime import datetime
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, ConfigDict

# --- Riot Account Schemas ---
class RiotAccountBase(BaseModel):
    riot_game_name: str
    riot_tag: str
    main_account: bool = False
    verified: bool = False

class RiotAccountCreate(BaseModel):
    riot_game_name: str
    riot_tag: str
    main_account: bool = False

class RiotAccountOut(RiotAccountBase):
    id: int
    player_id: int
    puuid: str
    summoner_id: Optional[str]
    solo_tier: Optional[str] = None
    solo_division: Optional[int] = None
    solo_league_points: Optional[int] = None
    solo_high_tier: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# --- Player Schemas ---
class PlayerBase(BaseModel):
    display_name: str
    discord_user_id: Optional[str] = None
    mmr: int = 1200

class PlayerCreate(PlayerBase):
    pass

class PlayerUpdate(BaseModel):
    display_name: Optional[str] = None
    discord_user_id: Optional[str] = None
    mmr: Optional[int] = None

class PlayerOut(PlayerBase):
    id: int
    created_at: datetime
    accounts: List[RiotAccountOut] = []
    model_config = ConfigDict(from_attributes=True)

# --- Match Schemas ---
class MatchParticipantOut(BaseModel):
    id: int
    match_id: str
    riot_account_id: Optional[int]
    player_id: Optional[int]
    summoner_name: str
    riot_tag: str
    champion_id: int
    champion_name: str
    win: bool
    kills: int
    deaths: int
    assists: int
    kda: float
    cs: int = 0
    total_damage_dealt: int = 0
    total_damage_dealt_to_champions: int
    total_damage_taken: int = 0
    damage_self_mitigated: int = 0
    total_heal: int = 0
    total_heals_on_teammates: int = 0
    total_damage_shielded_on_teammates: int = 0
    total_damage_dealt_to_objectives: int = 0
    total_damage_dealt_to_turrets: int = 0
    gold_earned: int
    vision_score: int = 0
    wards_placed: int = 0
    wards_killed: int = 0
    control_wards_bought: int = 0
    turret_kills: int = 0
    inhibitor_kills: int = 0
    double_kills: int = 0
    triple_kills: int = 0
    quadra_kills: int = 0
    penta_kills: int = 0
    killing_sprees: int = 0
    largest_killing_spree: int = 0
    largest_multi_kill: int = 0
    champ_level: int = 0
    time_ccing_others: int = 0
    total_minions_killed: int = 0
    neutral_minions_killed: int = 0
    ai_score: float
    team_id: int
    position: str
    model_config = ConfigDict(from_attributes=True)

class MatchOut(BaseModel):
    match_id: str
    game_creation: int
    game_duration: int
    game_mode: str
    synced_at: datetime
    participants: List[MatchParticipantOut] = []
    model_config = ConfigDict(from_attributes=True)

# --- Matchmaking Schemas ---
BalanceMode = Literal["mmr", "tier", "lane"]

class MatchmakeRequest(BaseModel):
    discord_user_ids: List[str]
    balance_mode: BalanceMode = "mmr"
    option_lane_priority: bool = False
    option_mmr_balance: bool = True
    option_recent_form: bool = False
    option_duo_separation: bool = False
    duo_pairs: Optional[List[List[str]]] = None

class TeamComposition(BaseModel):
    team_id: int  # 100: Blue, 200: Red
    players: List[PlayerOut]
    avg_mmr: float

class MatchmakeCommentaryRequest(BaseModel):
    blue_team: TeamComposition
    red_team: TeamComposition
    mmr_difference: float
    balance_mode: Optional[BalanceMode] = "mmr"

class MatchmakeResponse(BaseModel):
    blue_team: TeamComposition
    red_team: TeamComposition
    mmr_difference: float
    balance_mode: Optional[BalanceMode] = "mmr"
    balance_mode_label: Optional[str] = None
    ai_commentary: Optional[str] = None

# --- Player Analysis Schemas ---
class PlayerAnalysisMetricRow(BaseModel):
    metric: str
    label: str
    value: float
    formatted_value: str
    relative_rating: str
    relative_evaluation: str
    # 표시 보조 필드 — 스키마에 없으면 response_model이 걸러내므로 명시한다.
    desc: Optional[str] = None
    magnitude_rating: Optional[str] = None
    position_avg: Optional[float] = None
    formatted_position_avg: Optional[str] = None
    pct_vs_avg: Optional[int] = None

class PlayerAnalysisHighlight(BaseModel):
    label: str
    value: str
    position_avg: str
    rating: str
    description: str
    desc: Optional[str] = None

class PlayerPositionMetrics(BaseModel):
    position: str
    position_label: str
    games: int
    min_games: int
    locked: bool
    summary_table: List[PlayerAnalysisMetricRow] = []

class PlayerChampionStat(BaseModel):
    champion_id: int
    champion_name: str
    games: int
    win_rate: float
    kda: float
    cspm: float
    dpm: float
    gpm: float
    score: float

class PlayerChampionAnalysis(BaseModel):
    best: Optional[PlayerChampionStat] = None
    worst: Optional[PlayerChampionStat] = None
    table: List[PlayerChampionStat] = []

class PlayerWinLossComparison(BaseModel):
    metric: str
    label: str
    win_value: float
    loss_value: float
    diff: float
    impact: float
    higher_better: bool
    # 소비처가 그대로 출력하는 포맷 문자열 (예: "688", "43.3%", "+1.3").
    win_text: Optional[str] = None
    loss_text: Optional[str] = None
    diff_text: Optional[str] = None

class PlayerAnalysisReport(BaseModel):
    player_id: int
    matches: int
    main_position: str
    main_position_label: Optional[str] = None
    sub_position: Optional[str] = None
    sub_position_label: Optional[str] = None
    summary_table: List[PlayerAnalysisMetricRow]
    position_breakdown: List[PlayerPositionMetrics] = []
    strengths_top3: List[PlayerAnalysisHighlight]
    weaknesses_top3: List[PlayerAnalysisHighlight]
    win_patterns: List[str]
    loss_patterns: List[str]
    win_condition: str
    recommended_style: str
    player_type: str
    player_type_scores: dict[str, float]
    one_liner: str
    champion_analysis: PlayerChampionAnalysis
    win_loss_detail: List[PlayerWinLossComparison] = []
    # 신규 분석 섹션 — 데이터 부족 시 None.
    ping_profile: Optional[Dict[str, Any]] = None
    lane_performance: Optional[Dict[str, Any]] = None
    lane_final_analysis: Optional[Dict[str, Any]] = None
    recent_form: Optional[Dict[str, Any]] = None
    synergy: Optional[Dict[str, Any]] = None
    momentum: Optional[Dict[str, Any]] = None
