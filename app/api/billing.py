from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.security import get_current_user
from app.database import get_db
from app.models import NotificationChannel, NotificationLog, User
from app.schemas import CheckoutResponse, NotificationOut
from app.services import BillingService

router = APIRouter(tags=["billing", "notifications"])


@router.post("/billing/checkout", response_model=CheckoutResponse)
async def create_checkout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CheckoutResponse:
    return await BillingService(db, settings).create_checkout(user)


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
) -> dict:
    payload = await request.body()
    return await BillingService(db, settings).handle_webhook(payload, stripe_signature)


@router.get("/notifications", response_model=List[NotificationOut])
async def list_notifications(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[NotificationOut]:
    result = await db.execute(
        select(NotificationLog)
        .where(NotificationLog.user_id == user.id)
        .order_by(NotificationLog.created_at.desc())
        .limit(50)
    )
    return [NotificationOut.model_validate(n) for n in result.scalars().all()]


@router.get("/notifications/pending/desktop", response_model=List[NotificationOut])
async def pending_desktop(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[NotificationOut]:
    result = await db.execute(
        select(NotificationLog)
        .where(
            NotificationLog.user_id == user.id,
            NotificationLog.channel == NotificationChannel.desktop,
            NotificationLog.delivered.is_(False),
        )
        .order_by(NotificationLog.created_at.asc())
        .limit(20)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.delivered = True
    await db.commit()
    return [NotificationOut.model_validate(n) for n in rows]


@router.post("/notifications/{notification_id}/ack")
async def ack_notification(
    notification_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(NotificationLog).where(
            NotificationLog.id == notification_id,
            NotificationLog.user_id == user.id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.delivered = True
        await db.commit()
    return {"ok": True}
