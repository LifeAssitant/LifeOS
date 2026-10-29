import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.core.security import verify_password
from app.models import PasswordResetToken
from app.services import password_reset
from app.services.password_reset import PasswordResetService


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, user=None, reset_token=None):
        self.user = user
        self.reset_token = reset_token
        self.added = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    async def execute(self, statement):
        query = str(statement)
        if "FROM password_reset_tokens" in query and "SELECT" in query:
            return FakeResult(self.reset_token)
        if "FROM users" in query:
            return FakeResult(self.user)
        return FakeResult(None)

    async def commit(self):
        self.commits += 1


def configured_settings(**overrides):
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_from_email": "support@example.com",
        "smtp_starttls": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_password_recovery_requires_smtp_and_https_url():
    assert configured_settings().password_reset_ready
    assert not configured_settings(smtp_host="").password_reset_ready
    assert not configured_settings(public_app_url="http://localhost").password_reset_ready
    assert not configured_settings(smtp_username="user").password_reset_ready


def test_reset_request_sends_one_time_link_without_returning_token(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        email="person@example.com",
        password_hash="existing-hash",
        is_active=True,
    )
    db = FakeSession(user=user)
    sent = []
    monkeypatch.setattr(password_reset, "_send_reset_email", lambda *args: sent.append(args))

    asyncio.run(PasswordResetService(db, configured_settings()).request_reset(user.email))

    assert db.commits == 1
    assert len(db.added) == 1
    assert isinstance(db.added[0], PasswordResetToken)
    assert len(sent) == 1
    token = sent[0][2].split("token=", 1)[1]
    assert hashlib.sha256(token.encode()).hexdigest() == db.added[0].token_hash


def test_reset_password_changes_password_and_invalidates_sessions():
    raw_token = "one-time-reset-token-with-enough-entropy"
    reset_token = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        used_at=None,
    )
    user = SimpleNamespace(
        id=reset_token.user_id,
        is_active=True,
        password_hash="old-hash",
        auth_token_version=0,
    )
    db = FakeSession(user=user, reset_token=reset_token)

    asyncio.run(
        PasswordResetService(db, configured_settings()).reset_password(
            raw_token,
            "new-long-password",
        )
    )

    assert verify_password("new-long-password", user.password_hash)
    assert user.auth_token_version == 1
    assert reset_token.used_at is not None
    assert db.commits == 1


def test_reset_password_rejects_expired_or_unknown_tokens():
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            PasswordResetService(FakeSession(), configured_settings()).reset_password(
                "unknown-token-with-enough-length",
                "new-long-password",
            )
        )

    assert error.value.status_code == 400
