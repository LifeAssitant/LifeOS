from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.schemas import EventCreate, EventOut, EventUpdate
from app.services import EventService

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=List[EventOut])
async def list_events(
    from_dt: Optional[datetime] = Query(default=None, alias="from"),
    to_dt: Optional[datetime] = Query(default=None, alias="to"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[EventOut]:
    events = await EventService(db).list_events(user, from_dt=from_dt, to_dt=to_dt)
    return [EventOut.model_validate(e) for e in events]


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(
    payload: EventCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await EventService(db).create(user, payload)
    return EventOut.model_validate(event)


@router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await EventService(db).get(user, event_id)
    return EventOut.model_validate(event)


@router.patch("/{event_id}", response_model=EventOut)
async def update_event(
    event_id: UUID,
    payload: EventUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventOut:
    event = await EventService(db).update(user, event_id, payload)
    return EventOut.model_validate(event)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await EventService(db).delete(user, event_id)
