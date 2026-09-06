from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, BigInteger, Text
from sqlalchemy.orm import relationship
from app.database import Base

class Match(Base):
    __tablename__ = "matches"

    match_id = Column(String, primary_key=True, index=True)  # e.g., KR_8257574885
    game_creation = Column(BigInteger, nullable=False)      # epoch timestamp
    game_duration = Column(Integer, nullable=False)          # in seconds
    game_mode = Column(String, nullable=False)              # e.g., CUSTOM
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_data = Column(Text, nullable=True)

    # Relationships
    participants = relationship("MatchParticipant", back_populates="match", cascade="all, delete-orphan")

class MatchParticipant(Base):
    __tablename__ = "match_participants"

    id = Column(Integer, primary_key=True, index=True)
    match_id = Column(String, ForeignKey("matches.match_id", ondelete="CASCADE"), nullable=False)
    riot_account_id = Column(Integer, ForeignKey("riot_accounts.id", ondelete="SET NULL"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True)
    
    summoner_name = Column(String, nullable=False)
    riot_tag = Column(String, nullable=False)
    champion_id = Column(Integer, nullable=False)
    champion_name = Column(String, nullable=False)
    win = Column(Boolean, nullable=False)
    kills = Column(Integer, nullable=False)
    deaths = Column(Integer, nullable=False)
    assists = Column(Integer, nullable=False)
    kda = Column(Float, nullable=False)
    cs = Column(Integer, default=0, nullable=False)
    total_damage_dealt = Column(Integer, default=0, nullable=False)
    total_damage_dealt_to_champions = Column(Integer, nullable=False)
    total_damage_taken = Column(Integer, default=0, nullable=False)
    damage_self_mitigated = Column(Integer, default=0, nullable=False)
    total_heal = Column(Integer, default=0, nullable=False)
    total_heals_on_teammates = Column(Integer, default=0, nullable=False)
    total_damage_shielded_on_teammates = Column(Integer, default=0, nullable=False)
    total_damage_dealt_to_objectives = Column(Integer, default=0, nullable=False)
    total_damage_dealt_to_turrets = Column(Integer, default=0, nullable=False)
    gold_earned = Column(Integer, nullable=False)
    vision_score = Column(Integer, default=0, nullable=False)
    wards_placed = Column(Integer, default=0, nullable=False)
    wards_killed = Column(Integer, default=0, nullable=False)
    control_wards_bought = Column(Integer, default=0, nullable=False)
    turret_kills = Column(Integer, default=0, nullable=False)
    inhibitor_kills = Column(Integer, default=0, nullable=False)
    double_kills = Column(Integer, default=0, nullable=False)
    triple_kills = Column(Integer, default=0, nullable=False)
    quadra_kills = Column(Integer, default=0, nullable=False)
    penta_kills = Column(Integer, default=0, nullable=False)
    killing_sprees = Column(Integer, default=0, nullable=False)
    largest_killing_spree = Column(Integer, default=0, nullable=False)
    largest_multi_kill = Column(Integer, default=0, nullable=False)
    champ_level = Column(Integer, default=0, nullable=False)
    time_ccing_others = Column(Integer, default=0, nullable=False)
    total_minions_killed = Column(Integer, default=0, nullable=False)
    neutral_minions_killed = Column(Integer, default=0, nullable=False)
    ai_score = Column(Float, nullable=False)                # DeepLOL AI-Score
    team_id = Column(Integer, nullable=False)               # 100: Blue, 200: Red
    position = Column(String, default="UNKNOWN", nullable=False)  # TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY, UNKNOWN
    raw_data = Column(Text, nullable=True)

    # Relationships
    match = relationship("Match", back_populates="participants")
    riot_account = relationship("RiotAccount", back_populates="match_performances")
    player = relationship("Player", back_populates="match_performances")
