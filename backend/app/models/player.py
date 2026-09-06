from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, index=True)
    discord_user_id = Column(String, unique=True, index=True, nullable=True)
    display_name = Column(String, nullable=False)
    mmr = Column(Integer, default=1200, nullable=False)
    initial_mmr = Column(Integer, default=1200, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    accounts = relationship("RiotAccount", back_populates="player", cascade="all, delete-orphan")
    match_performances = relationship("MatchParticipant", back_populates="player")
    discord_aliases = relationship("PlayerDiscordAlias", back_populates="player", cascade="all, delete-orphan")

class PlayerDiscordAlias(Base):
    __tablename__ = "player_discord_aliases"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    discord_user_id = Column(String, unique=True, index=True, nullable=False)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    player = relationship("Player", back_populates="discord_aliases")
