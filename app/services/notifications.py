from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    Event,
    NotificationChannel,
    NotificationKind,
    NotificationLog,
    Task,
    TaskStatus,
    User,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _in_quiet_hours(user: User, now: datetime) -> bool:
    if not user.quiet_hours_enabled or not user.quiet_hours_start or not user.quiet_hours_end:
        return False
    current = now.timetz().replace(tzinfo=None)
    start: time = user.quiet_hours_start
    end: time = user.quiet_hours_end
    if start <= end:
        return start <= current <= end
    # Overnight window (e.g. 22:00–07:00)
    return current >= start or current <= end


class NotificationService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def log(
        self,
        *,
        user: User,
        kind: NotificationKind,
        channel: NotificationChannel,
        title: str,
        body: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[UUID] = None,
        delivered: bool = False,
        meta: Optional[dict] = None,
    ) -> NotificationLog:
        entry = NotificationLog(
            user_id=user.id,
            kind=kind,
            channel=channel,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
            delivered=delivered,
            meta=meta,
        )
        self.db.add(entry)
        await self.db.commit()
        await self.db.refresh(entry)
        return entry

    async def send_expo(self, token: str, title: str, body: str, data: Optional[dict] = None) -> bool:
        headers = {"Content-Type": "application/json"}
        if self.settings.expo_access_token:
            headers["Authorization"] = f"Bearer {self.settings.expo_access_token}"
        message = {
            "to": token,
            "sound": "default",
            "title": title,
            "body": body,
            "data": data or {},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://exp.host/--/api/v2/push/send",
                json=message,
                headers=headers,
            )
            return response.is_success

    async def notify_user(
        self,
        user: User,
        *,
        kind: NotificationKind,
        title: str,
        body: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[UUID] = None,
    ) -> None:
        if _in_quiet_hours(user, _utcnow()):
            await self.log(
                user=user,
                kind=kind,
                channel=NotificationChannel.log,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
                delivered=False,
                meta={"skipped": "quiet_hours"},
            )
            return

        delivered_any = False
        if user.expo_push_token:
            ok = await self.send_expo(
                user.expo_push_token,
                title,
                body,
                {"kind": kind.value, "entity_type": entity_type, "entity_id": str(entity_id) if entity_id else None},
            )
            await self.log(
                user=user,
                kind=kind,
                channel=NotificationChannel.expo,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
                delivered=ok,
            )
            delivered_any = delivered_any or ok

        if user.desktop_push_token:
            # Desktop polls /notifications/pending; we still log for delivery.
            await self.log(
                user=user,
                kind=kind,
                channel=NotificationChannel.desktop,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
                delivered=False,
                meta={"desktop_token": user.desktop_push_token},
            )
            delivered_any = True

        if not delivered_any:
            await self.log(
                user=user,
                kind=kind,
                channel=NotificationChannel.log,
                title=title,
                body=body,
                entity_type=entity_type,
                entity_id=entity_id,
                delivered=False,
            )


class ReminderScanner:
    """Finds due / overdue items and dispatches notifications."""

    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.notifications = NotificationService(db, settings)

    async def run_once(self) -> int:
        now = _utcnow()
        sent = 0
        sent += await self._scan_tasks(now)
        sent += await self._scan_events(now)
        sent += await self._scan_still_open(now)
        return sent

    async def _scan_tasks(self, now: datetime) -> int:
        result = await self.db.execute(
            select(Task, User)
            .join(User, User.id == Task.user_id)
            .where(
                Task.status == TaskStatus.open,
                Task.remind_at.is_not(None),
                Task.remind_at <= now,
                or_(Task.last_reminded_at.is_(None), Task.last_reminded_at < Task.remind_at),
            )
        )
        count = 0
        for task, user in result.all():
            kind = NotificationKind.overdue if task.due_at and task.due_at < now else NotificationKind.due_soon
            title = "Task due soon" if kind == NotificationKind.due_soon else "Task overdue"
            await self.notifications.notify_user(
                user,
                kind=kind,
                title=title,
                body=task.title,
                entity_type="task",
                entity_id=task.id,
            )
            task.last_reminded_at = now
            count += 1
        await self.db.commit()
        return count

    async def _scan_events(self, now: datetime) -> int:
        result = await self.db.execute(
            select(Event, User)
            .join(User, User.id == Event.user_id)
            .where(
                Event.remind_at.is_not(None),
                Event.remind_at <= now,
                or_(Event.last_reminded_at.is_(None), Event.last_reminded_at < Event.remind_at),
            )
        )
        count = 0
        for event, user in result.all():
            await self.notifications.notify_user(
                user,
                kind=NotificationKind.due_soon,
                title="Event starting soon",
                body=event.title,
                entity_type="event",
                entity_id=event.id,
            )
            event.last_reminded_at = now
            count += 1
        await self.db.commit()
        return count

    async def _scan_still_open(self, now: datetime) -> int:
        """Nudge if a task is still open well past due and last reminded > 2h ago."""
        result = await self.db.execute(
            select(Task, User)
            .join(User, User.id == Task.user_id)
            .where(
                Task.status == TaskStatus.open,
                Task.due_at.is_not(None),
                Task.due_at < now,
                or_(
                    Task.last_reminded_at.is_(None),
                    Task.last_reminded_at < now.replace(microsecond=0),
                ),
            )
        )
        count = 0
        for task, user in result.all():
            if task.last_reminded_at and (now - task.last_reminded_at).total_seconds() < 2 * 3600:
                continue
            # Only still_open if already past due by > 30 minutes
            if task.due_at and (now - task.due_at).total_seconds() < 30 * 60:
                continue
            await self.notifications.notify_user(
                user,
                kind=NotificationKind.still_open,
                title="Still open?",
                body=f"Did you finish “{task.title}”? Tap to mark it done.",
                entity_type="task",
                entity_id=task.id,
            )
            task.last_reminded_at = now
            count += 1
        await self.db.commit()
        return count
