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
    # Haiku 4.5 (octobre 2025+) — l'ancien claude-3-5-haiku-20241022 a été déprécié
    chat_model: str = "claude-haiku-4-5"
    chat_rate_limit_per_hour: int = 20  # par user

    # Whitelist des ligues pour value bets 1X2.
    # - Ligue 1 + Bundesliga : modèle global 17k (Ligue 1 +22.1%, Bundesliga +3.3%)
    # - Serie A : modèle per-league dédié (+5.87%, 157 paris, hit 36.3%)
    value_bet_leagues: list[str] = ["Ligue 1", "Bundesliga", "Serie A", "World Cup"]

    # Whitelist O/U 2.5 — DÉSACTIVÉ après tracking 2 ans (-8% ROI sur 95 paris).
    # Le modèle OU n'apporte pas de value vs le marché. À ré-activer si retrain
    # avec features dédiées (xG, BTTS, etc.) donne de meilleurs résultats.
    value_bet_ou_leagues: list[str] = []

    # Whitelist Asian Handicap — backtest 2020-2025 (10609 matchs, edge 10-20%).
    # Plus gros marché en volume : ~870 value bets/an.
    # Serie A +5.8%, Ligue 1 +5.1%, Premier League +3.6% — 3 ligues profitables.
    value_bet_ah_leagues: list[str] = ["Ligue 1", "Premier League", "Serie A", "World Cup"]

    # Ligues qui utilisent leur modèle per-league plutôt que le global.
    # Backtest a montré que seule Serie A en bénéficie (le global est meilleur ailleurs).
    per_league_model_leagues: list[str] = ["Serie A"]

    # Filtres value betting calibrés par le tracking 2 ans (958 paris settled).
    # Sweet spot empirique = edge ∈ [5%, 20%] : ROI +5.5% vs +3.5% à 8% (sample
    # x1.4 et drawdown 46% vs 60%). Cap haut 20% filtre les cotes overpriced.
    #
    # ⏰ REVIEW DUE : 2026-06-28 (6 semaines après changement edge_min 0.08→0.05).
    # Ouvrir /tracking fenêtre 60j et comparer ROI live à edge 5% avec le tableau
    # historique (+5.5%). Si ROI live < +3%, le sweet spot était gonflé par leak
    # DC → remonter à 0.08 et refit DC en rolling-window. Voir tâche #11.
    value_bet_edge_min: float = 0.05
    value_bet_edge_max: float = 0.20

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
