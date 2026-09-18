from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "LifeOS"
    app_env: str = "development"
    debug: bool = True
    api_prefix: str = "/api/v1"
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:8081",
            "exp://127.0.0.1:8081",
        ]
    )

    database_url: str = "postgresql+asyncpg://lifeos:lifeos@localhost:5432/lifeos"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "change-me-to-a-long-random-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    supabase_url: str = ""
    supabase_jwt_secret: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_calendar_redirect_uri: str = (
        "http://127.0.0.1:8000/api/v1/calendar/google/callback"
    )
    desktop_oauth_success_url: str = "lifeos://auth/calendar-connected"

    fernet_key: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    hosted_chat_credit_cost: int = 1
    free_starter_credits: int = 50

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_credits: str = ""
    stripe_success_url: str = "http://localhost:5173/settings?credits=success"
    stripe_cancel_url: str = "http://localhost:5173/settings?credits=cancel"
    credits_per_purchase: int = 100

    expo_access_token: str = ""

    reminder_poll_seconds: int = 30
    default_remind_before_minutes: int = 15

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
