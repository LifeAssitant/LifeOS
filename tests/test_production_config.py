import pytest
from cryptography.fernet import Fernet

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
        database_url="postgresql+asyncpg://user:password@db.example.com/lifeos",
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
        database_url="postgresql://user:password@db.example.com/lifeos",
    )
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    app = create_app()

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
