from datetime import datetime, time
from typing import Any, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import AIMode, ChatRole, NotificationChannel, NotificationKind, TaskSource, TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth ---


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class GoogleAuthRequest(BaseModel):
    access_token: str = Field(min_length=20, max_length=8000)


# --- User / settings ---


class UserPublic(ORMModel):
    id: UUID
    email: EmailStr
    display_name: Optional[str]
    onboarding_completed: bool
    ai_mode: AIMode
    credit_balance: int
    has_byok_key: bool = False
    remind_before_minutes: int
    quiet_hours_enabled: bool
    quiet_hours_start: Optional[time]
    quiet_hours_end: Optional[time]
    google_calendar_connected: bool = False
    profession: Optional[str] = None
    professions: Optional[list[str]] = None
    age: Optional[int] = None
    busy_level: Optional[int] = None
    use_cases: Optional[list[str]] = None
    profile_completed: bool = False


class ProfileSurvey(BaseModel):
    """Audience questions answered once, between onboarding and the walkthrough."""

    professions: Optional[list[str]] = Field(default=None, max_length=12)
    age: Optional[int] = Field(default=None, ge=5, le=120)
    busy_level: Optional[int] = Field(default=None, ge=1, le=5)
    use_cases: Optional[list[str]] = Field(default=None, max_length=12)

    @field_validator("use_cases", "professions")
    @classmethod
    def clean_list(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = [item.strip()[:80] for item in value if item and item.strip()]
        return cleaned or None


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    onboarding_completed: Optional[bool] = None
    remind_before_minutes: Optional[int] = Field(default=None, ge=0, le=24 * 60)
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[time] = None
    quiet_hours_end: Optional[time] = None
    expo_push_token: Optional[str] = Field(default=None, max_length=255)
    desktop_push_token: Optional[str] = Field(default=None, max_length=255)


class AISettingsUpdate(BaseModel):
    ai_mode: AIMode
    gemini_api_key: Optional[str] = Field(default=None, min_length=10, max_length=512)

    @field_validator("gemini_api_key")
    @classmethod
    def strip_key(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


# --- Tasks ---


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: Optional[str] = None
    due_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None
    source: TaskSource = TaskSource.manual


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    notes: Optional[str] = None
    due_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None
    status: Optional[TaskStatus] = None


class TaskOut(ORMModel):
    id: UUID
    title: str
    notes: Optional[str]
    due_at: Optional[datetime]
    remind_at: Optional[datetime]
    status: TaskStatus
    source: TaskSource
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


# --- Events ---


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    notes: Optional[str] = None
    location: Optional[str] = Field(default=None, max_length=300)
    start_at: datetime
    end_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None
    source: TaskSource = TaskSource.manual


class EventUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    notes: Optional[str] = None
    location: Optional[str] = Field(default=None, max_length=300)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    remind_at: Optional[datetime] = None


class EventOut(ORMModel):
    id: UUID
    title: str
    notes: Optional[str]
    location: Optional[str]
    start_at: datetime
    end_at: Optional[datetime]
    remind_at: Optional[datetime]
    source: TaskSource
    external_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class GoogleCalendarStatus(BaseModel):
    connected: bool


class GoogleCalendarConnectResponse(BaseModel):
    url: str


class GoogleCalendarSyncResponse(BaseModel):
    synced: int
    removed: int


# --- Today ---


class TodayResponse(BaseModel):
    tasks: List[TaskOut]
    events: List[EventOut]


# --- Chat ---


class ChatSendRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    timezone: Optional[str] = Field(
        default=None,
        max_length=64,
        description="IANA timezone from the client, e.g. Asia/Kolkata",
    )


class ChatAction(BaseModel):
    type: Literal[
        "create_task",
        "update_task",
        "complete_task",
        "create_event",
        "update_event",
        "delete_task",
        "delete_event",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    entity_id: Optional[UUID] = None
    summary: str = ""


class ChatMessageOut(ORMModel):
    id: UUID
    role: ChatRole
    content: str
    actions: Optional[list[dict[str, Any]]] = None
    linked_entity_ids: Optional[list[str]] = None
    created_at: datetime


class ChatUndoRequest(BaseModel):
    message_id: UUID
    action_index: int = 0


# --- Billing ---


class CheckoutResponse(BaseModel):
    checkout_url: str


class CreditsResponse(BaseModel):
    credit_balance: int
    ai_mode: AIMode


# --- Notifications ---


class NotificationOut(ORMModel):
    id: UUID
    kind: NotificationKind
    channel: NotificationChannel
    title: str
    body: str
    entity_type: Optional[str]
    entity_id: Optional[UUID]
    delivered: bool
    created_at: datetime


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
