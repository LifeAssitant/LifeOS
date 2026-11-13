from functools import lru_cache
from typing import List

from cryptography.fernet import Fernet
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_UNSAFE_JWT_SECRETS = {
    "",
    "change-me-to-a-long-random-secret",
    "secret",
    "changeme",
}


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

    jwt_secret: str = "change-me-to-a-long-random-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True
    public_app_url: str = "https://brainlesshuman.com"

    supabase_url: str = ""
    supabase_jwt_secret: str = ""
    supabase_service_role_key: str = ""

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
    # Earn garden/AI credits by finishing work: 1 credit every N completions.
    task_credit_interval: int = 50
    task_credit_reward: int = 1

    billing_enabled: bool = False
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_monthly_plan_id: str = ""
    razorpay_monthly_price_paise: int = 0
    subscription_weekly_credits: int = 0
    subscription_total_cycles: int = 120
    razorpay_topup_amount_paise: int = 0
    razorpay_topup_credits: int = 0

    google_play_billing_enabled: bool = False
    google_play_package_name: str = "com.lifeos.app"
    google_play_topup_product_id: str = ""
    google_play_topup_credits: int = 20
    google_play_service_account_email: str = ""
    google_play_service_account_private_key: str = ""

    expo_access_token: str = ""

    reminder_poll_seconds: int = 30
    default_remind_before_minutes: int = 15
    # Silence window before check-in (prompt gap + push nudge). Midpoint of 5–6h.
    chat_check_in_hours: float = 5.5

    # Langfuse conversation traces. Both keys must be set before anything is exported.
    # US region: https://us.cloud.langfuse.com
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_base_url: str = "https://us.cloud.langfuse.com"

    # Optional extra OpenTelemetry export. Langfuse does not need these.
    otel_traces_enabled: bool = False
    otel_service_name: str = "lifeos-api"
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: str = ""
    otel_traces_console: bool = False
    # When true, spans store the user message and assistant reply.
    trace_capture_content: bool = False

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"development", "dev", "local"}

    def validate_production(self) -> None:
        if self.is_dev:
            return

        problems: list[str] = []
        if self.debug:
            problems.append("DEBUG must be false")
        if (
            self.jwt_secret.strip().lower() in _UNSAFE_JWT_SECRETS
            or len(self.jwt_secret) < 32
        ):
            problems.append("JWT_SECRET must be a strong secret of at least 32 characters")
        if not self.fernet_key:
            problems.append("FERNET_KEY is required")
        else:
            try:
                Fernet(self.fernet_key.encode())
            except (ValueError, TypeError):
                problems.append("FERNET_KEY must be a valid Fernet key")
        if not self.cors_origins or "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must explicitly list trusted origins")
        elif not any(origin.startswith("https://") for origin in self.cors_origins):
            problems.append("Production CORS_ORIGINS must include an HTTPS website origin")
        elif any(
            origin != "null" and not origin.startswith("https://")
            for origin in self.cors_origins
        ):
            problems.append("Production CORS_ORIGINS must use HTTPS")
        if not self.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
            problems.append("DATABASE_URL must use PostgreSQL")
        if self.supabase_url and not self.supabase_service_role_key:
            problems.append(
                "SUPABASE_SERVICE_ROLE_KEY is required when Supabase authentication is enabled"
            )
        if self.billing_enabled and not self.razorpay_checkout_ready:
            problems.append(
                "Razorpay credentials and valid top-up prices are required when billing is enabled"
            )
        if self.razorpay_monthly_plan_id and not self.razorpay_billing_ready:
            problems.append(
                "Complete Razorpay subscription and webhook configuration is required when a monthly plan is configured"
            )
        if self.google_play_billing_enabled:
            if not self.google_play_billing_ready:
                problems.append(
                    "Google Play billing requires a package name, product ID, positive credit amount, "
                    "and service-account credentials"
                )
            else:
                try:
                    private_key = serialization.load_pem_private_key(
                        self.google_play_service_account_private_key.replace(
                            "\\n", "\n"
                        ).encode(),
                        password=None,
                    )
                except (TypeError, ValueError, UnsupportedAlgorithm):
                    problems.append(
                        "GOOGLE_PLAY_SERVICE_ACCOUNT_PRIVATE_KEY must be a valid PEM private key"
                    )
                else:
                    if not isinstance(private_key, RSAPrivateKey):
                        problems.append(
                            "GOOGLE_PLAY_SERVICE_ACCOUNT_PRIVATE_KEY must be an RSA key"
                        )

        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))

    @property
    def razorpay_billing_ready(self) -> bool:
        return all(
            (
                self.razorpay_key_id,
                self.razorpay_key_secret,
                self.razorpay_webhook_secret,
                self.razorpay_monthly_plan_id,
                self.razorpay_monthly_price_paise > 0,
                self.subscription_weekly_credits > 0,
                self.razorpay_topup_amount_paise > 0,
                self.razorpay_topup_credits > 0,
                self.subscription_total_cycles > 0,
            )
        )

    @property
    def razorpay_checkout_ready(self) -> bool:
        return bool(
            self.razorpay_key_id
            and self.razorpay_key_secret
            and self.razorpay_topup_amount_paise >= 100
            and self.razorpay_topup_credits > 0
        )

    @property
    def google_play_billing_ready(self) -> bool:
        return bool(
            self.google_play_billing_enabled
            and self.google_play_package_name
            and self.google_play_topup_product_id
            and self.google_play_topup_credits > 0
            and self.google_play_service_account_email
            and self.google_play_service_account_private_key
        )

    @property
    def password_reset_ready(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_from_email
            and self.public_app_url.startswith("https://")
            and bool(self.smtp_username) == bool(self.smtp_password)
            and (self.smtp_use_ssl or self.smtp_starttls)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
