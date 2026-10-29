import asyncio
import hashlib
import logging
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.security import hash_password
from app.models import PasswordResetToken, User

logger = logging.getLogger(__name__)
RESET_TOKEN_LIFETIME = timedelta(minutes=30)
RESET_REQUEST_COOLDOWN = timedelta(minutes=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PasswordResetService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def request_reset(self, email: str) -> None:
        if not self.settings.password_reset_ready:
            raise HTTPException(
                status_code=503,
                detail="Password recovery is not configured",
            )

        result = await self.db.execute(
            select(User).where(User.email == email.lower(), User.is_active.is_(True))
        )
        user = result.scalar_one_or_none()
        if user is None or user.password_hash is None:
            return

        now = _utcnow()
        recent = await self.db.execute(
            select(PasswordResetToken.id).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.created_at >= now - RESET_REQUEST_COOLDOWN,
            )
        )
        if recent.scalar_one_or_none() is not None:
            return

        await self.db.execute(
            delete(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
        )

        raw_token = secrets.token_urlsafe(32)
        reset_token = PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=now + RESET_TOKEN_LIFETIME,
        )
        self.db.add(reset_token)
        await self.db.commit()

        reset_url = (
            self.settings.public_app_url.rstrip("/")
            + "/reset-password?token="
            + quote(raw_token, safe="")
        )
        await asyncio.to_thread(
            _send_reset_email,
            self.settings,
            user.email,
            reset_url,
        )

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        result = await self.db.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > _utcnow(),
            )
            .with_for_update()
        )
        reset_token = result.scalar_one_or_none()
        if reset_token is None:
            raise HTTPException(
                status_code=400,
                detail="This password reset link is invalid or has expired",
            )

        user_result = await self.db.execute(
            select(User).where(User.id == reset_token.user_id, User.is_active.is_(True))
        )
        user = user_result.scalar_one_or_none()
        if user is None or user.password_hash is None:
            raise HTTPException(
                status_code=400,
                detail="This password reset link is invalid or has expired",
            )

        user.password_hash = hash_password(new_password)
        user.auth_token_version += 1
        reset_token.used_at = _utcnow()
        await self.db.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
        await self.db.commit()


def _send_reset_email(settings: Settings, recipient: str, reset_url: str) -> None:
    message = EmailMessage()
    message["Subject"] = "Reset your LifeOS password"
    message["From"] = settings.smtp_from_email
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your LifeOS password.\n\n"
        f"Use this link within 30 minutes:\n{reset_url}\n\n"
        "If you did not request this, you can ignore this email."
    )

    context = ssl.create_default_context()
    if settings.smtp_use_ssl:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=20,
            context=context,
        ) as server:
            _authenticate_and_send(server, settings, message)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_starttls:
            server.starttls(context=context)
        _authenticate_and_send(server, settings, message)


def _authenticate_and_send(
    server: smtplib.SMTP,
    settings: Settings,
    message: EmailMessage,
) -> None:
    if settings.smtp_username:
        server.login(settings.smtp_username, settings.smtp_password)
    server.send_message(message)
