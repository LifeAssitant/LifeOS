from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.encryption import SecretBox
from app.models import Event, TaskSource, User

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_google_oauth(settings: Settings) -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)",
        )


def build_connect_state(user_id: UUID, settings: Settings) -> str:
    return jwt.encode(
        {
            "sub": str(user_id),
            "type": "google_calendar",
            "exp": _utcnow() + timedelta(minutes=15),
            "iat": _utcnow(),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def parse_connect_state(state: str, settings: Settings) -> UUID:
    try:
        payload = jwt.decode(
            state, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("type") != "google_calendar":
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        return UUID(str(payload["sub"]))
    except (JWTError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc


def build_authorize_url(user_id: UUID, settings: Settings) -> str:
    _require_google_oauth(settings)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_calendar_redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": build_connect_state(user_id, settings),
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str, settings: Settings) -> dict[str, Any]:
    _require_google_oauth(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_calendar_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if res.status_code >= 400:
        raise HTTPException(status_code=400, detail="Google token exchange failed")
    return res.json()


async def refresh_access_token(refresh_token: str, settings: Settings) -> str:
    _require_google_oauth(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if res.status_code >= 400:
        raise HTTPException(status_code=400, detail="Google token refresh failed")
    data = res.json()
    access = data.get("access_token")
    if not access:
        raise HTTPException(status_code=400, detail="Google did not return an access token")
    return str(access)


def _parse_google_dt(value: Optional[dict[str, Any]]) -> Optional[datetime]:
    if not value:
        return None
    if value.get("dateTime"):
        text = str(value["dateTime"]).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if value.get("date"):
        # All-day: treat as midnight UTC on that date
        return datetime.fromisoformat(f"{value['date']}T00:00:00+00:00")
    return None


class GoogleCalendarService:
    def __init__(
        self, db: AsyncSession, settings: Settings, secret_box: SecretBox
    ) -> None:
        self.db = db
        self.settings = settings
        self.secret_box = secret_box

    async def save_refresh_token(self, user: User, refresh_token: str) -> User:
        user.encrypted_google_refresh_token = self.secret_box.encrypt(refresh_token)
        user.google_calendar_connected = True
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def disconnect(self, user: User) -> None:
        user.encrypted_google_refresh_token = None
        user.google_calendar_connected = False
        result = await self.db.execute(
            select(Event).where(
                Event.user_id == user.id, Event.source == TaskSource.google
            )
        )
        for event in result.scalars().all():
            await self.db.delete(event)
        await self.db.commit()

    async def sync(self, user: User) -> tuple[int, int]:
        if not user.encrypted_google_refresh_token:
            raise HTTPException(status_code=400, detail="Google Calendar is not connected")

        refresh_token = self.secret_box.decrypt(user.encrypted_google_refresh_token)
        access_token = await refresh_access_token(refresh_token, self.settings)

        now = _utcnow()
        time_min = (now - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        time_max = (now + timedelta(days=90)).isoformat().replace("+00:00", "Z")

        items: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        async with httpx.AsyncClient(timeout=45) as client:
            while True:
                params: dict[str, Any] = {
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "maxResults": 250,
                }
                if page_token:
                    params["pageToken"] = page_token
                res = await client.get(
                    GOOGLE_EVENTS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if res.status_code >= 400:
                    raise HTTPException(
                        status_code=502, detail="Failed to fetch Google Calendar events"
                    )
                data = res.json()
                items.extend(data.get("items") or [])
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

        existing = await self.db.execute(
            select(Event).where(
                Event.user_id == user.id, Event.source == TaskSource.google
            )
        )
        by_external = {
            e.external_id: e for e in existing.scalars().all() if e.external_id
        }
        seen: set[str] = set()
        synced = 0

        for raw in items:
            external_id = str(raw.get("id") or "").strip()
            if not external_id or raw.get("status") == "cancelled":
                continue
            start_at = _parse_google_dt(raw.get("start"))
            if start_at is None:
                continue
            end_at = _parse_google_dt(raw.get("end"))
            title = (raw.get("summary") or "Busy").strip()[:300] or "Busy"
            notes = raw.get("description")
            location = raw.get("location")
            if location:
                location = str(location)[:300]

            seen.add(external_id)
            event = by_external.get(external_id)
            if event is None:
                event = Event(
                    user_id=user.id,
                    title=title,
                    notes=notes,
                    location=location,
                    start_at=start_at,
                    end_at=end_at,
                    source=TaskSource.google,
                    external_id=external_id,
                )
                self.db.add(event)
            else:
                event.title = title
                event.notes = notes
                event.location = location
                event.start_at = start_at
                event.end_at = end_at
            synced += 1

        removed = 0
        for external_id, event in by_external.items():
            if external_id not in seen:
                await self.db.delete(event)
                removed += 1

        await self.db.commit()
        return synced, removed
