import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.main import create_app


def test_development_configuration_keeps_local_defaults():
    Settings(app_env="development").validate_production()


def test_production_configuration_requires_hardened_values():
    settings = Settings(app_env="production")

    with pytest.raises(ValueError, match="Unsafe production configuration"):
        settings.validate_production()


def test_production_configuration_accepts_explicit_secure_values():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="server-only-service-role-key",
        database_url="postgresql+asyncpg://user:password@db.example.com/lifeos",
    )

    settings.validate_production()


def test_production_billing_requires_topup_configuration():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        database_url="postgresql+asyncpg://db.example.com/lifeos",
        supabase_url="",
        billing_enabled=True,
        razorpay_key_id="",
        razorpay_key_secret="",
        razorpay_topup_amount_paise=0,
        razorpay_topup_credits=0,
    )

    with pytest.raises(ValueError, match="Razorpay credentials"):
        settings.validate_production()


def test_production_topup_billing_does_not_require_subscription_setup():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        database_url="postgresql+asyncpg://db.example.com/lifeos",
        supabase_url="",
        billing_enabled=True,
        razorpay_key_id="rzp_test_id",
        razorpay_key_secret="test_secret",
        razorpay_topup_amount_paise=9900,
        razorpay_topup_credits=20,
    )

    settings.validate_production()


def test_production_google_play_billing_requires_service_account_configuration():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        database_url="postgresql+asyncpg://db.example.com/lifeos",
        supabase_url="",
        google_play_billing_enabled=True,
    )

    with pytest.raises(ValueError, match="Google Play billing requires"):
        settings.validate_production()


def test_production_google_play_billing_requires_valid_rsa_key():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        database_url="postgresql+asyncpg://db.example.com/lifeos",
        supabase_url="",
        google_play_billing_enabled=True,
        google_play_topup_product_id="lifeos_credit_topup_20",
        google_play_service_account_email="lifeos@example.iam.gserviceaccount.com",
        google_play_service_account_private_key="not-a-private-key",
    )

    with pytest.raises(ValueError, match="valid PEM private key"):
        settings.validate_production()


def test_production_google_play_billing_accepts_complete_rsa_configuration():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoded_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        database_url="postgresql+asyncpg://db.example.com/lifeos",
        supabase_url="",
        google_play_billing_enabled=True,
        google_play_topup_product_id="lifeos_credit_topup_20",
        google_play_service_account_email="lifeos@example.iam.gserviceaccount.com",
        google_play_service_account_private_key=encoded_private_key,
    )

    settings.validate_production()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"debug": True}, "DEBUG"),
        ({"jwt_secret": "short"}, "JWT_SECRET"),
        ({"fernet_key": ""}, "FERNET_KEY"),
        ({"fernet_key": "not-a-fernet-key"}, "valid Fernet key"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"cors_origins": ["http://localhost:5173"]}, "HTTPS"),
        ({"cors_origins": ["null"]}, "HTTPS website origin"),
        ({"database_url": "sqlite+aiosqlite:///lifeos.db"}, "PostgreSQL"),
        (
            {
                "supabase_url": "https://project.supabase.co",
                "supabase_service_role_key": "",
            },
            "SUPABASE_SERVICE_ROLE_KEY",
        ),
    ],
)
def test_production_configuration_reports_individual_unsafe_values(
    overrides: dict, expected: str
):
    values = {
        "app_env": "production",
        "debug": False,
        "jwt_secret": "a-secure-random-secret-with-at-least-32-characters",
        "fernet_key": Fernet.generate_key().decode(),
        "cors_origins": ["https://brainlesshuman.com"],
        "supabase_url": "https://project.supabase.co",
        "supabase_service_role_key": "server-only-service-role-key",
        "database_url": "postgresql+asyncpg://user:password@db.example.com/lifeos",
    }
    values.update(overrides)
    settings = Settings(**values)

    with pytest.raises(ValueError, match=expected):
        settings.validate_production()


def test_production_configuration_allows_the_electron_file_origin():
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com", "null"],
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="server-only-service-role-key",
        database_url="postgresql://user:password@db.example.com/lifeos",
    )

    settings.validate_production()


def test_production_disables_public_api_documentation(monkeypatch):
    settings = Settings(
        app_env="production",
        debug=False,
        jwt_secret="a-secure-random-secret-with-at-least-32-characters",
        fernet_key=Fernet.generate_key().decode(),
        cors_origins=["https://brainlesshuman.com"],
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="server-only-service-role-key",
        database_url="postgresql://user:password@db.example.com/lifeos",
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    app = create_app()

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
