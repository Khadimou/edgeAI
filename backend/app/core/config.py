from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    environment: str = "development"
    secret_key: str = "dev_secret_key_change_in_production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    database_url: str = "postgresql://edgeai:edgeai_secret@localhost:5432/edgeai"
    redis_url: str = "redis://localhost:6379"

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""
    stripe_price_elite: str = ""

    football_data_api_key: str = ""
    odds_api_key: str = ""

    mlflow_tracking_uri: str = "sqlite:///mlflow.db"

    rate_limit_per_minute: int = 100
    rate_limit_sensitive_per_minute: int = 10

    sentry_dsn: str = ""

    # Chatbot pédagogique (glossaire edgeAI via Anthropic Claude Haiku)
    anthropic_api_key: str = ""
    chat_model: str = "claude-3-5-haiku-20241022"
    chat_rate_limit_per_hour: int = 20  # par user

    # Whitelist des ligues pour value bets 1X2.
    # - Ligue 1 + Bundesliga : modèle global 17k (Ligue 1 +22.1%, Bundesliga +3.3%)
    # - Serie A : modèle per-league dédié (+5.87%, 157 paris, hit 36.3%)
    value_bet_leagues: list[str] = ["Ligue 1", "Bundesliga", "Serie A"]

    # Whitelist O/U 2.5 — issu du backtest sur le modèle 17k samples.
    # Seule la Premier League reste profitable (+5.7%, 175 paris, hit 47.4%) avec
    # le modèle entraîné sur la nouvelle base de 17k matchs.
    value_bet_ou_leagues: list[str] = ["Premier League"]

    # Whitelist Asian Handicap — backtest 2020-2025 (10609 matchs, edge 10-20%).
    # Plus gros marché en volume : ~870 value bets/an.
    # Serie A +5.8%, Ligue 1 +5.1%, Premier League +3.6% — 3 ligues profitables.
    value_bet_ah_leagues: list[str] = ["Ligue 1", "Premier League", "Serie A"]

    # Ligues qui utilisent leur modèle per-league plutôt que le global.
    # Backtest a montré que seule Serie A en bénéficie (le global est meilleur ailleurs).
    per_league_model_leagues: list[str] = ["Serie A"]

    # Filtres value betting calibrés par le backtest (edge ∈ [8%, 20%] = sweet spot)
    value_bet_edge_min: float = 0.08
    value_bet_edge_max: float = 0.20

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
