from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.encryption import SecretBox
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import AIMode, Event, Task, TaskStatus, User
from app.schemas import (
    AISettingsUpdate,
    EventCreate,
    EventUpdate,
    LoginRequest,
    RegisterRequest,
    TaskCreate,
    TaskUpdate,
    UserPublic,
    UserUpdate,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_to_public(user: User) -> UserPublic:
    return UserPublic(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        onboarding_completed=user.onboarding_completed,
        ai_mode=user.ai_mode,
        credit_balance=user.credit_balance,
        has_byok_key=bool(user.encrypted_gemini_key),
        remind_before_minutes=user.remind_before_minutes,
        quiet_hours_enabled=user.quiet_hours_enabled,
        quiet_hours_start=user.quiet_hours_start,
        quiet_hours_end=user.quiet_hours_end,
    )


class AuthService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def register(self, payload: RegisterRequest) -> tuple[User, str, str]:
        existing = await self.db.execute(select(User).where(User.email == payload.email.lower()))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already registered")

        user = User(
            email=payload.email.lower(),
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            credit_balance=self.settings.free_starter_credits,
            ai_mode=AIMode.hosted,
            remind_before_minutes=self.settings.default_remind_before_minutes,
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user, create_access_token(user.id, self.settings), create_refresh_token(
            user.id, self.settings
        )

    async def login(self, payload: LoginRequest) -> tuple[User, str, str]:
        result = await self.db.execute(select(User).where(User.email == payload.email.lower()))
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        return user, create_access_token(user.id, self.settings), create_refresh_token(
            user.id, self.settings
        )

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        try:
            payload = decode_token(refresh_token, self.settings)
            if payload.get("type") != "refresh":
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            user_id = UUID(payload["sub"])
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid refresh token") from exc

        result = await self.db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found")
        return create_access_token(user.id, self.settings), create_refresh_token(
            user.id, self.settings
        )


class UserService:
    def __init__(self, db: AsyncSession, secret_box: SecretBox) -> None:
        self.db = db
        self.secret_box = secret_box

    async def update_profile(self, user: User, payload: UserUpdate) -> User:
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(user, key, value)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def update_ai_settings(self, user: User, payload: AISettingsUpdate) -> User:
        user.ai_mode = payload.ai_mode
        if payload.ai_mode == AIMode.byok:
            if payload.gemini_api_key:
                user.encrypted_gemini_key = self.secret_box.encrypt(payload.gemini_api_key)
            elif not user.encrypted_gemini_key:
                raise HTTPException(
                    status_code=400,
                    detail="Gemini API key required for BYOK mode",
                )
        await self.db.commit()
        await self.db.refresh(user)
        return user

    def resolve_gemini_key(self, user: User, settings: Settings) -> str:
        if user.ai_mode == AIMode.byok:
            if not user.encrypted_gemini_key:
                raise HTTPException(status_code=400, detail="No BYOK key configured")
            return self.secret_box.decrypt(user.encrypted_gemini_key)
        if not settings.gemini_api_key:
            raise HTTPException(
                status_code=503,
                detail="Hosted Gemini is not configured on the server",
            )
        return settings.gemini_api_key


def _default_remind_at(
    due_or_start: Optional[datetime],
    remind_at: Optional[datetime],
    minutes: int,
) -> Optional[datetime]:
    if remind_at is not None:
        return remind_at
    if due_or_start is None:
        return None
    return due_or_start - timedelta(minutes=minutes)


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_tasks(
        self,
        user: User,
        *,
        status: Optional[TaskStatus] = None,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
    ) -> list[Task]:
        stmt: Select[tuple[Task]] = select(Task).where(Task.user_id == user.id)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if from_dt is not None:
            stmt = stmt.where(or_(Task.due_at.is_(None), Task.due_at >= from_dt))
        if to_dt is not None:
            stmt = stmt.where(or_(Task.due_at.is_(None), Task.due_at <= to_dt))
        stmt = stmt.order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get(self, user: User, task_id: UUID) -> Task:
        result = await self.db.execute(
            select(Task).where(Task.id == task_id, Task.user_id == user.id)
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    async def create(self, user: User, payload: TaskCreate) -> Task:
        task = Task(
            user_id=user.id,
            title=payload.title.strip(),
            notes=payload.notes,
            due_at=payload.due_at,
            remind_at=_default_remind_at(
                payload.due_at, payload.remind_at, user.remind_before_minutes
            ),
            source=payload.source,
            status=TaskStatus.open,
        )
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def update(self, user: User, task_id: UUID, payload: TaskUpdate) -> Task:
        task = await self.get(user, task_id)
        data = payload.model_dump(exclude_unset=True)
        if "status" in data and data["status"] == TaskStatus.done and task.status != TaskStatus.done:
            task.completed_at = _utcnow()
        if "status" in data and data["status"] == TaskStatus.open:
            task.completed_at = None
        for key, value in data.items():
            setattr(task, key, value)
        if "due_at" in data and "remind_at" not in data:
            task.remind_at = _default_remind_at(
                task.due_at, None, user.remind_before_minutes
            )
        await self.db.commit()
        await self.db.refresh(task)
        return task

    async def complete(self, user: User, task_id: UUID) -> Task:
        return await self.update(user, task_id, TaskUpdate(status=TaskStatus.done))

    async def delete(self, user: User, task_id: UUID) -> None:
        task = await self.get(user, task_id)
        await self.db.delete(task)
        await self.db.commit()


class EventService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_events(
        self,
        user: User,
        *,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
    ) -> list[Event]:
        stmt = select(Event).where(Event.user_id == user.id)
        if from_dt is not None:
            stmt = stmt.where(Event.start_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(Event.start_at <= to_dt)
        stmt = stmt.order_by(Event.start_at.asc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get(self, user: User, event_id: UUID) -> Event:
        result = await self.db.execute(
            select(Event).where(Event.id == event_id, Event.user_id == user.id)
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    async def create(self, user: User, payload: EventCreate) -> Event:
        event = Event(
            user_id=user.id,
            title=payload.title.strip(),
            notes=payload.notes,
            location=payload.location,
            start_at=payload.start_at,
            end_at=payload.end_at,
            remind_at=_default_remind_at(
                payload.start_at, payload.remind_at, user.remind_before_minutes
            ),
            source=payload.source,
        )
        self.db.add(event)
        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def update(self, user: User, event_id: UUID, payload: EventUpdate) -> Event:
        event = await self.get(user, event_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(event, key, value)
        if "start_at" in data and "remind_at" not in data:
            event.remind_at = _default_remind_at(
                event.start_at, None, user.remind_before_minutes
            )
        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def delete(self, user: User, event_id: UUID) -> None:
        event = await self.get(user, event_id)
        await self.db.delete(event)
        await self.db.commit()


class TodayService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.tasks = TaskService(db)
        self.events = EventService(db)

    async def get_today(self, user: User) -> tuple[list[Task], list[Event]]:
        now = _utcnow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        open_tasks = await self.tasks.list_tasks(user, status=TaskStatus.open)
        # Include open tasks due today or overdue without due date filter for "today focus"
        today_tasks = [
            t
            for t in open_tasks
            if t.due_at is None or (t.due_at < end)
        ]
        events = await self.events.list_events(user, from_dt=start, to_dt=end)
        return today_tasks, events
