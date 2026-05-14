from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from enum import Enum


class Plan(str, Enum):
    FREE = "FREE"
    PRO = "PRO"
    ELITE = "ELITE"


class RiskProfile(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class BetOutcome(str, Enum):
    HOME = "HOME"
    DRAW = "DRAW"
    AWAY = "AWAY"
    OVER = "OVER"
    UNDER = "UNDER"


class BetStatus(str, Enum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    CASHOUT = "CASHOUT"


class MatchStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    FINISHED = "FINISHED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"


# --- Auth ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    user: "UserResponse"


class RefreshRequest(BaseModel):
    refresh_token: str


# --- User ---

class UserProfileUpdate(BaseModel):
    name: str | None = None
    bankroll: float | None = Field(None, ge=0)
    risk_profile: RiskProfile | None = None
    kelly_fraction: float | None = Field(None, ge=0.1, le=1.0)
    max_bets_per_day: int | None = Field(None, ge=1, le=10)
    alerts_enabled: bool | None = None
    goal_amount: float | None = Field(None, ge=0)
    goal_timeframe_days: int | None = Field(None, ge=1, le=365)
    goal_start_date: datetime | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    name: str | None
    plan: Plan
    bankroll: float
    risk_profile: RiskProfile
    kelly_fraction: float
    max_bets_per_day: int
    alerts_enabled: bool
    referral_code: str | None
    goal_amount: float | None = None
    goal_timeframe_days: int | None = None
    goal_start_date: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# --- Matches ---

class PredictionOut(BaseModel):
    prob_home: float
    prob_draw: float
    prob_away: float
    confidence: float
    shap_values: dict | None = None
    model_version: str
    computed_at: datetime


class MatchOut(BaseModel):
    id: str
    external_id: str
    league: str
    season: str
    home_team: str
    away_team: str
    match_date: datetime
    status: MatchStatus
    home_score: int | None
    away_score: int | None
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    venue: str | None
    prediction: PredictionOut | None = None

    class Config:
        from_attributes = True


# --- Recommendations ---

class RecommendationOut(BaseModel):
    id: str
    match_id: str
    outcome: BetOutcome
    edge: float
    kelly_stake: float
    recommended_amount: float
    odds: float
    strategy: str | None
    expires_at: datetime | None
    created_at: datetime
    match: MatchOut | None = None

    class Config:
        from_attributes = True


# --- Bets ---

class BetCreate(BaseModel):
    match_id: str
    recommendation_id: str | None = None
    outcome: BetOutcome
    amount: float = Field(gt=0)
    odds: float = Field(gt=1.0)
    bookmaker: str | None = None
    notes: str | None = None


class BetResultUpdate(BaseModel):
    status: BetStatus
    home_score: int | None = None
    away_score: int | None = None


class BetOut(BaseModel):
    id: str
    match_id: str
    recommendation_id: str | None
    outcome: BetOutcome
    amount: float
    odds: float
    status: BetStatus
    profit_loss: float | None
    bookmaker: str | None
    notes: str | None
    placed_at: datetime
    settled_at: datetime | None
    match: MatchOut | None = None

    class Config:
        from_attributes = True


# --- Bankroll ---

class BankrollHistoryOut(BaseModel):
    id: str
    amount: float
    balance: float
    event_type: str
    reference_id: str | None
    timestamp: datetime

    class Config:
        from_attributes = True


class BankrollStats(BaseModel):
    current_balance: float
    total_deposited: float
    total_profit_loss: float
    roi_percent: float
    history: list[BankrollHistoryOut]


# --- Stats ---

class PerformanceStats(BaseModel):
    total_bets: int
    won: int
    lost: int
    pending: int
    win_rate: float
    roi_percent: float
    total_profit_loss: float
    avg_odds: float
    expected_value_realized: float
    best_streak: int
    current_streak: int
    by_league: dict[str, dict]
    by_outcome: dict[str, dict]
    monthly_pnl: list[dict]


# --- Match Analysis ---

class MatchAnalysis(BaseModel):
    match: MatchOut
    prediction: PredictionOut
    recommendation: RecommendationOut | None
    home_form: dict
    away_form: dict
    h2h: dict
    value_assessment: dict
