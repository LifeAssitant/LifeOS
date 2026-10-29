import logging
import smtplib

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.database import get_db
from app.core.rate_limit import RateLimiter
from app.schemas import (
    GoogleAuthRequest,
    HealthResponse,
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetMessage,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services import AuthService
from app.services.password_reset import PasswordResetService

router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)
password_reset_limiter = RateLimiter(
    max_calls=5,
    window_seconds=900,
    message="Too many password recovery requests. Try again later.",
)


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(status="ok", app=settings.app_name, version=__version__)


@router.post("/auth/register", response_model=TokenResponse)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    _, access, refresh = await AuthService(db, settings).register(payload)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    _, access, refresh = await AuthService(db, settings).login(payload)
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/auth/google", response_model=TokenResponse)
async def google_auth(
    payload: GoogleAuthRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    _, access, refresh = await AuthService(db, settings).login_with_google(
        payload.access_token
    )
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    access, refresh_token = await AuthService(db, settings).refresh(payload.refresh_token)
    return TokenResponse(access_token=access, refresh_token=refresh_token)


@router.post(
    "/auth/password-reset/request",
    response_model=PasswordResetMessage,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PasswordResetMessage:
    client_ip = request.client.host if request.client else "unknown"
    password_reset_limiter.check(client_ip)
    try:
        await PasswordResetService(db, settings).request_reset(str(payload.email))
    except (smtplib.SMTPException, OSError):
        logger.exception("Password reset email delivery failed")
    return PasswordResetMessage(
        message="If an account supports password recovery, a reset link will be sent."
    )


@router.post(
    "/auth/password-reset/confirm",
    response_model=PasswordResetMessage,
)
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PasswordResetMessage:
    await PasswordResetService(db, settings).reset_password(
        payload.token,
        payload.password,
    )
    return PasswordResetMessage(message="Your password has been reset. Sign in again.")
