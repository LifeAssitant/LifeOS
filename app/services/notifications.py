from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    ChatMessage,
    ChatRole,
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
        data: Optional[dict] = None,
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
        expo_data = {
            "kind": kind.value,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            **(data or {}),
        }
        if user.expo_push_token:
            ok = await self.send_expo(
                user.expo_push_token,
                title,
                body,
                expo_data,
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
        sent += await self._scan_chat_check_ins(now)
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

    async def _scan_chat_check_ins(self, now: datetime) -> int:
        """Nudge users who have been quiet in chat for chat_check_in_hours."""
        hours = float(self.settings.chat_check_in_hours)
        threshold = now - timedelta(hours=hours)

        last_user_msg = (
            select(
                ChatMessage.user_id.label("user_id"),
                func.max(ChatMessage.created_at).label("last_at"),
            )
            .where(ChatMessage.role == ChatRole.user)
            .group_by(ChatMessage.user_id)
            .subquery()
        )

        result = await self.db.execute(
            select(User, last_user_msg.c.last_at)
            .outerjoin(last_user_msg, last_user_msg.c.user_id == User.id)
            .where(
                User.is_active.is_(True),
                User.onboarding_completed.is_(True),
                or_(
                    User.expo_push_token.is_not(None),
                    User.desktop_push_token.is_not(None),
                ),
                or_(
                    User.last_check_in_notified_at.is_(None),
                    User.last_check_in_notified_at <= threshold,
                ),
                or_(
                    last_user_msg.c.last_at.is_(None),
                    last_user_msg.c.last_at <= threshold,
                ),
                or_(
                    last_user_msg.c.last_at.is_not(None),
                    User.created_at <= threshold,
                ),
            )
            .limit(50)
        )

        count = 0
        for user, _last_at in result.all():
            if _in_quiet_hours(user, now):
                # Retry on a later poll once quiet hours end.
                continue
            body = await self._check_in_body(user, now)
            await self.notifications.notify_user(
                user,
                kind=NotificationKind.check_in,
                title="LifeOS",
                body=body,
                data={"screen": "chat"},
            )
            user.last_check_in_notified_at = now
            count += 1
        await self.db.commit()
        return count

    async def _check_in_body(self, user: User, now: datetime) -> str:
        """Personalized nudge using today's open load."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Approximate local day with UTC day boundaries; good enough for nudge copy.
        day_end = day_start + timedelta(days=1)

        open_tasks = await self.db.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.user_id == user.id, Task.status == TaskStatus.open)
        )
        task_n = int(open_tasks.scalar_one() or 0)

        today_events = await self.db.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.user_id == user.id,
                Event.start_at >= day_start,
                Event.start_at < day_end,
            )
        )
        event_n = int(today_events.scalar_one() or 0)

        if task_n or event_n:
            bits: list[str] = []
            if task_n:
                bits.append(f"{task_n} open task{'s' if task_n != 1 else ''}")
            if event_n:
                bits.append(f"{event_n} event{'s' if event_n != 1 else ''} today")
            return f"You have {' and '.join(bits)}. Want to plan the next few hours?"
        return "How's the day going — want to plan the next few hours?"
