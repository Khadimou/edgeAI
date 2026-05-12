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

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
