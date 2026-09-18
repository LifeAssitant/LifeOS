import enum
import uuid
from datetime import datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base

# JSON that works on Postgres (JSONB) and falls back for other dialects in tests.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class AIMode(str, enum.Enum):
    hosted = "hosted"
    byok = "byok"


class TaskStatus(str, enum.Enum):
    open = "open"
    done = "done"


class TaskSource(str, enum.Enum):
    chat = "chat"
    manual = "manual"


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class NotificationChannel(str, enum.Enum):
    expo = "expo"
    desktop = "desktop"
    log = "log"


class NotificationKind(str, enum.Enum):
    due_soon = "due_soon"
    overdue = "overdue"
    still_open = "still_open"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    ai_mode: Mapped[AIMode] = mapped_column(
        Enum(AIMode, name="ai_mode", native_enum=False, length=16),
        default=AIMode.hosted,
        nullable=False,
    )
    encrypted_gemini_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credit_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    remind_before_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quiet_hours_start: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    quiet_hours_end: Mapped[Optional[time]] = mapped_column(Time, nullable=True)

    expo_push_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    desktop_push_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    events: Mapped[list["Event"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["NotificationLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    remind_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=False, length=16),
        default=TaskStatus.open,
        nullable=False,
    )
    source: Mapped[TaskSource] = mapped_column(
        Enum(TaskSource, name="task_source", native_enum=False, length=16),
        default=TaskSource.manual,
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="tasks")


class Event(Base, TimestampMixin):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    remind_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source: Mapped[TaskSource] = mapped_column(
        Enum(TaskSource, name="event_source", native_enum=False, length=16),
        default=TaskSource.manual,
        nullable=False,
    )
    last_reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="events")


class ChatMessage(Base, TimestampMixin):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[ChatRole] = mapped_column(
        Enum(ChatRole, name="chat_role", native_enum=False, length=16),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    actions: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    linked_entity_ids: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)

    user: Mapped["User"] = relationship(back_populates="chat_messages")


class NotificationLog(Base, TimestampMixin):
    __tablename__ = "notification_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, name="notification_kind", native_enum=False, length=32),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(NotificationChannel, name="notification_channel", native_enum=False, length=16),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    user: Mapped["User"] = relationship(back_populates="notifications")
