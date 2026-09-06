from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class RiotAccount(Base):
    __tablename__ = "riot_accounts"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    riot_game_name = Column(String, nullable=False)
    riot_tag = Column(String, nullable=False)
    puuid = Column(String, unique=True, index=True, nullable=False)
    summoner_id = Column(String, nullable=True)
    main_account = Column(Boolean, default=False, nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    solo_tier = Column(String, nullable=True)
    solo_division = Column(Integer, nullable=True)
    solo_league_points = Column(Integer, nullable=True)
    solo_high_tier = Column(String, nullable=True)
    solo_tier_updated_at = Column(DateTime, nullable=True)
    # 동기화 효율화용 추적 필드
    last_seen_match_id = Column(String, nullable=True)   # 마지막으로 확인한 최신 경기 (변화 감지)
    last_synced_at = Column(DateTime, nullable=True)      # 마지막으로 동기화를 시도한 시각
    last_active_at = Column(DateTime, nullable=True)      # 마지막으로 새 경기가 확인된 시각 (활동/휴면 판정)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    player = relationship("Player", back_populates="accounts")
    match_performances = relationship("MatchParticipant", back_populates="riot_account")
