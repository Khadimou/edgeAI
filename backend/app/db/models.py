"""
Modèles SQLAlchemy — miroir exact du schéma Prisma.
Le schéma est géré via `prisma push` (CLI Node) ; SQLAlchemy lit/écrit uniquement.
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, JSON, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import enum


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────

class Plan(str, enum.Enum):
    FREE = "FREE"
    PRO = "PRO"
    ELITE = "ELITE"


class RiskProfile(str, enum.Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class MatchStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    FINISHED = "FINISHED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"


class BetOutcome(str, enum.Enum):
    HOME = "HOME"
    DRAW = "DRAW"
    AWAY = "AWAY"
    OVER = "OVER"
    UNDER = "UNDER"


class BetStatus(str, enum.Enum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    CASHOUT = "CASHOUT"


class EventType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    BET_PLACED = "BET_PLACED"
    BET_WON = "BET_WON"
    BET_LOST = "BET_LOST"
    BET_VOID = "BET_VOID"
    SUBSCRIPTION_CREDIT = "SUBSCRIPTION_CREDIT"
    REFERRAL_BONUS = "REFERRAL_BONUS"


# ── Tables ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    supabase_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    name: Mapped[Optional[str]] = mapped_column(String)
    plan: Mapped[str] = mapped_column(String, default="FREE")
    bankroll: Mapped[float] = mapped_column(Float, default=0.0)
    risk_profile: Mapped[str] = mapped_column(String, default="MODERATE")
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0.25)
    max_bets_per_day: Mapped[int] = mapped_column(Integer, default=3)
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, unique=True)
    referral_code: Mapped[Optional[str]] = mapped_column(String, unique=True)
    referred_by: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    recommendations: Mapped[list["Recommendation"]] = relationship(back_populates="user")
    bets: Mapped[list["Bet"]] = relationship(back_populates="user")
    bankroll_history: Mapped[list["BankrollHistory"]] = relationship(back_populates="user")


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    external_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    league: Mapped[str] = mapped_column(String, nullable=False)
    season: Mapped[str] = mapped_column(String, nullable=False)
    home_team: Mapped[str] = mapped_column(String, nullable=False)
    away_team: Mapped[str] = mapped_column(String, nullable=False)
    match_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, default="SCHEDULED")
    home_score: Mapped[Optional[int]] = mapped_column(Integer)
    away_score: Mapped[Optional[int]] = mapped_column(Integer)
    home_odds: Mapped[Optional[float]] = mapped_column(Float)
    draw_odds: Mapped[Optional[float]] = mapped_column(Float)
    away_odds: Mapped[Optional[float]] = mapped_column(Float)
    venue: Mapped[Optional[str]] = mapped_column(String)
    round: Mapped[Optional[str]] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    predictions: Mapped[list["Prediction"]] = relationship(back_populates="match", order_by="Prediction.computed_at.desc()")
    recommendations: Mapped[list["Recommendation"]] = relationship(back_populates="match")
    bets: Mapped[list["Bet"]] = relationship(back_populates="match")


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    match_id: Mapped[str] = mapped_column(String, ForeignKey("matches.id"), nullable=False)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    prob_home: Mapped[float] = mapped_column(Float, nullable=False)
    prob_draw: Mapped[float] = mapped_column(Float, nullable=False)
    prob_away: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    shap_values: Mapped[Optional[dict]] = mapped_column(JSON)
    features: Mapped[Optional[dict]] = mapped_column(JSON)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    match: Mapped["Match"] = relationship(back_populates="predictions")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    match_id: Mapped[str] = mapped_column(String, ForeignKey("matches.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    edge: Mapped[float] = mapped_column(Float, nullable=False)
    kelly_stake: Mapped[float] = mapped_column(Float, nullable=False)
    recommended_amount: Mapped[float] = mapped_column(Float, nullable=False)
    odds: Mapped[float] = mapped_column(Float, nullable=False)
    strategy: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="recommendations")
    match: Mapped["Match"] = relationship(back_populates="recommendations")
    bets: Mapped[list["Bet"]] = relationship(back_populates="recommendation")


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    match_id: Mapped[str] = mapped_column(String, ForeignKey("matches.id"), nullable=False)
    recommendation_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("recommendations.id"))
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    odds: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    profit_loss: Mapped[Optional[float]] = mapped_column(Float)
    bookmaker: Mapped[Optional[str]] = mapped_column(String)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="bets")
    match: Mapped["Match"] = relationship(back_populates="bets")
    recommendation: Mapped[Optional["Recommendation"]] = relationship(back_populates="bets")


class BankrollHistory(Base):
    __tablename__ = "bankroll_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    reference_id: Mapped[Optional[str]] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="bankroll_history")


class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_urlsafe(16))
    version: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    log_loss: Mapped[float] = mapped_column(Float, nullable=False)
    brier_score: Mapped[float] = mapped_column(Float, nullable=False)
    features_hash: Mapped[str] = mapped_column(String, nullable=False)
    artifact_path: Mapped[str] = mapped_column(String, nullable=False)
    is_deployed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSON)
