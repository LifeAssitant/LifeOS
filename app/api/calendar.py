from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.encryption import get_secret_box
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import (
    GoogleCalendarConnectResponse,
    GoogleCalendarStatus,
    GoogleCalendarSyncResponse,
)
from app.services.google_calendar import (
    GoogleCalendarService,
    build_authorize_url,
    exchange_code_for_tokens,
    parse_connect_state,
)

router = APIRouter(prefix="/calendar/google", tags=["calendar"])


def _service(db: AsyncSession, settings: Settings) -> GoogleCalendarService:
    return GoogleCalendarService(db, settings, get_secret_box())


@router.get("/status", response_model=GoogleCalendarStatus)
async def google_calendar_status(
    user: User = Depends(get_current_user),
) -> GoogleCalendarStatus:
    return GoogleCalendarStatus(connected=bool(user.google_calendar_connected))


@router.get("/connect", response_model=GoogleCalendarConnectResponse)
async def google_calendar_connect(
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> GoogleCalendarConnectResponse:
    return GoogleCalendarConnectResponse(url=build_authorize_url(user.id, settings))


@router.get("/callback")
async def google_calendar_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    user_id = parse_connect_state(state, settings)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    tokens = await exchange_code_for_tokens(code, settings)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # Re-consent may omit refresh_token if already granted; keep existing if present.
        if not user.encrypted_google_refresh_token:
            raise HTTPException(
                status_code=400,
                detail="Google did not return a refresh token. Disconnect and try again.",
            )
    else:
        await _service(db, settings).save_refresh_token(user, str(refresh_token))

    user.google_calendar_connected = True
    await db.commit()
    return RedirectResponse(url=settings.desktop_oauth_success_url, status_code=302)


@router.post("/sync", response_model=GoogleCalendarSyncResponse)
async def google_calendar_sync(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> GoogleCalendarSyncResponse:
    synced, removed = await _service(db, settings).sync(user)
    return GoogleCalendarSyncResponse(synced=synced, removed=removed)


@router.delete("", response_model=GoogleCalendarStatus)
async def google_calendar_disconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> GoogleCalendarStatus:
    await _service(db, settings).disconnect(user)
    return GoogleCalendarStatus(connected=False)
