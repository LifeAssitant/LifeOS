from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.database import get_db
from app.schemas import (
    GoogleAuthRequest,
    HealthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services import AuthService

router = APIRouter(tags=["auth"])


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
