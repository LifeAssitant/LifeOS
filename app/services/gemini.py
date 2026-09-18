from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from google import genai
from google.genai import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.rate_limit import enforce_hosted_chat_limit
from app.models import AIMode, ChatMessage, ChatRole, TaskSource, TaskStatus, User
from app.schemas import EventCreate, EventUpdate, TaskCreate, TaskUpdate
from app.services.lifecycle import EventService, TaskService, UserService


SYSTEM_PROMPT = """You are LifeOS, a calm and cute life manager assistant.
Help the user manage tasks and events only. Be warm, brief, and clear.
When the user asks to create, update, complete, or delete tasks/events, respond with:
1) A short friendly message
2) A JSON actions block

Use exactly this format at the end of your reply when taking actions:

```json
{"actions":[{"type":"create_task","summary":"Added Study","payload":{"title":"Study","due_at":"2026-09-18T18:00:00+05:30"}}]}
```

Allowed action types:
- create_task: payload {title, notes?, due_at?}
- update_task: payload {task_id, title?, notes?, due_at?, status?}
- complete_task: payload {task_id}
- delete_task: payload {task_id}
- create_event: payload {title, start_at, end_at?, location?, notes?}
- update_event: payload {event_id, title?, start_at?, end_at?, location?, notes?}
- delete_event: payload {event_id}

Rules:
- Always use the user's current local date/time and timezone from the context block.
- "today", "tonight", "tomorrow" must resolve against that local clock — never invent a past year.
- ISO-8601 datetimes MUST include the user's timezone offset (e.g. +05:30 for Asia/Kolkata).
  If the user says 11:15 PM, write 23:15 with their offset — do NOT tag local clock times as +00:00 / Z.
- For update_task / update_event, only include fields the user wants changed. Never send null for title.
- Match existing items by the IDs in the schedule context when updating or completing.
- If no action is needed, omit the JSON block.
- Never invent unrelated productivity features.
- Prefer one clear action over many.
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_tz(name: Optional[str]) -> ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo("UTC")


def _system_prompt_with_clock(tz_name: Optional[str]) -> str:
    tz = _resolve_tz(tz_name)
    now_local = datetime.now(tz)
    offset = now_local.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"[User clock]\n"
        f"- timezone: {getattr(tz, 'key', tz_name) or 'UTC'}\n"
        f"- local_now: {now_local.isoformat()}\n"
        f"- utc_offset: {offset_fmt}\n"
        f"- Use this clock for every relative date/time.\n"
    )


def _extract_actions_block(text: str) -> tuple[str, list[dict[str, Any]]]:
    fence = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    raw_json = None
    cleaned = text
    if fence:
        raw_json = fence.group(1)
        cleaned = (text[: fence.start()] + text[fence.end() :]).strip()
    else:
        match = re.search(r"(\{\s*\"actions\"\s*:\s*\[.*\]\s*\})\s*$", text, re.DOTALL)
        if match:
            raw_json = match.group(1)
            cleaned = text[: match.start()].strip()

    actions: list[dict[str, Any]] = []
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            actions = list(parsed.get("actions") or [])
        except json.JSONDecodeError:
            actions = []
    return cleaned, actions


def _build_contents(history: list[ChatMessage], prompt: str) -> list[types.Content]:
    contents: list[types.Content] = []
    for msg in history[:-1]:
        role = "user" if msg.role == ChatRole.user else "model"
        contents.append(
            types.Content(role=role, parts=[types.Part.from_text(text=msg.content)])
        )
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
    return contents


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()
    # Fallback for alternate SDK shapes
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = candidates[0].content.parts
            return "".join(getattr(p, "text", "") or "" for p in parts).strip()
    except Exception:
        pass
    return ""


class GeminiChatService:
    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        user_service: UserService,
    ) -> None:
        self.db = db
        self.settings = settings
        self.user_service = user_service
        self.tasks = TaskService(db)
        self.events = EventService(db)

    async def history(self, user: User, limit: int = 40) -> list[ChatMessage]:
        result = await self.db.execute(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return rows

    async def _charge_if_hosted(self, user: User) -> None:
        if user.ai_mode != AIMode.hosted:
            return
        enforce_hosted_chat_limit(user.id)
        cost = self.settings.hosted_chat_credit_cost
        if user.credit_balance < cost:
            raise HTTPException(
                status_code=402,
                detail="Not enough AI credits. Buy credits or switch to your own Gemini key.",
            )
        user.credit_balance -= cost
        await self.db.commit()
        await self.db.refresh(user)

    async def _context_snapshot(self, user: User) -> str:
        open_tasks = await self.tasks.list_tasks(user, status=TaskStatus.open)
        # Wide window so wrongly dated past items stay matchable for updates
        recent_events = await self.events.list_events(
            user, from_dt=_utcnow() - timedelta(days=60)
        )
        task_lines = [
            f"- [{t.id}] {t.title} due={t.due_at.isoformat() if t.due_at else 'none'}"
            for t in open_tasks[:20]
        ]
        event_lines = [
            f"- [{e.id}] {e.title} start={e.start_at.isoformat()}"
            for e in recent_events[:30]
        ]
        return (
            f"Open tasks:\n{chr(10).join(task_lines) or '- none'}\n\n"
            f"Recent/upcoming events:\n{chr(10).join(event_lines) or '- none'}"
        )

    def _task_update_payload(
        self, payload: dict[str, Any], default_tz: ZoneInfo
    ) -> TaskUpdate:
        data: dict[str, Any] = {}
        if "title" in payload and payload["title"] is not None:
            data["title"] = payload["title"]
        if "notes" in payload:
            data["notes"] = payload["notes"]
        if "due_at" in payload:
            data["due_at"] = _parse_dt(payload.get("due_at"), default_tz=default_tz)
        if "status" in payload and payload["status"]:
            data["status"] = TaskStatus(payload["status"])
        return TaskUpdate(**data)

    def _event_update_payload(
        self, payload: dict[str, Any], default_tz: ZoneInfo
    ) -> EventUpdate:
        data: dict[str, Any] = {}
        if "title" in payload and payload["title"] is not None:
            data["title"] = payload["title"]
        if "notes" in payload:
            data["notes"] = payload["notes"]
        if "location" in payload:
            data["location"] = payload["location"]
        if "start_at" in payload:
            data["start_at"] = _parse_dt(payload.get("start_at"), default_tz=default_tz)
        if "end_at" in payload:
            data["end_at"] = _parse_dt(payload.get("end_at"), default_tz=default_tz)
        return EventUpdate(**data)

    async def apply_actions(
        self,
        user: User,
        actions: list[dict[str, Any]],
        *,
        timezone_name: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        applied: list[dict[str, Any]] = []
        linked: list[str] = []
        default_tz = _resolve_tz(timezone_name)

        for raw in actions:
            action_type = raw.get("type")
            payload = raw.get("payload") or {}
            summary = raw.get("summary") or ""
            entity_id: Optional[str] = None

            try:
                if action_type == "create_task":
                    task = await self.tasks.create(
                        user,
                        TaskCreate(
                            title=payload["title"],
                            notes=payload.get("notes"),
                            due_at=_parse_dt(payload.get("due_at"), default_tz=default_tz),
                            source=TaskSource.chat,
                        ),
                    )
                    entity_id = str(task.id)
                    summary = summary or f"Added task “{task.title}”"
                elif action_type == "update_task":
                    task_id = UUID(str(payload["task_id"]))
                    task = await self.tasks.update(
                        user,
                        task_id,
                        self._task_update_payload(payload, default_tz),
                    )
                    entity_id = str(task.id)
                    summary = summary or f"Updated “{task.title}”"
                elif action_type == "complete_task":
                    task = await self.tasks.complete(user, UUID(str(payload["task_id"])))
                    entity_id = str(task.id)
                    summary = summary or f"Completed “{task.title}”"
                elif action_type == "delete_task":
                    task_id = UUID(str(payload["task_id"]))
                    await self.tasks.delete(user, task_id)
                    entity_id = str(task_id)
                    summary = summary or "Deleted task"
                elif action_type == "create_event":
                    event = await self.events.create(
                        user,
                        EventCreate(
                            title=payload["title"],
                            notes=payload.get("notes"),
                            location=payload.get("location"),
                            start_at=_parse_dt(payload["start_at"], default_tz=default_tz)
                            or _utcnow(),
                            end_at=_parse_dt(payload.get("end_at"), default_tz=default_tz),
                            source=TaskSource.chat,
                        ),
                    )
                    entity_id = str(event.id)
                    summary = summary or f"Added event “{event.title}”"
                elif action_type == "update_event":
                    event_id = UUID(str(payload["event_id"]))
                    event = await self.events.update(
                        user,
                        event_id,
                        self._event_update_payload(payload, default_tz),
                    )
                    entity_id = str(event.id)
                    summary = summary or f"Updated “{event.title}”"
                elif action_type == "delete_event":
                    event_id = UUID(str(payload["event_id"]))
                    await self.events.delete(user, event_id)
                    entity_id = str(event_id)
                    summary = summary or "Deleted event"
                else:
                    continue
            except Exception:
                await self.db.rollback()
                continue

            record = {
                "type": action_type,
                "summary": summary,
                "payload": payload,
                "entity_id": entity_id,
                "undoable": action_type
                in {"create_task", "create_event", "complete_task", "update_task", "update_event"},
            }
            applied.append(record)
            if entity_id:
                linked.append(entity_id)

        return applied, linked

    async def undo_action(self, user: User, message_id: UUID, action_index: int) -> None:
        result = await self.db.execute(
            select(ChatMessage).where(
                ChatMessage.id == message_id,
                ChatMessage.user_id == user.id,
                ChatMessage.role == ChatRole.assistant,
            )
        )
        message = result.scalar_one_or_none()
        if message is None or not message.actions:
            raise HTTPException(status_code=404, detail="Action not found")
        if action_index < 0 or action_index >= len(message.actions):
            raise HTTPException(status_code=400, detail="Invalid action index")

        action = message.actions[action_index]
        action_type = action.get("type")
        entity_id = action.get("entity_id")
        if not entity_id:
            raise HTTPException(status_code=400, detail="Action cannot be undone")

        eid = UUID(str(entity_id))
        if action_type == "create_task":
            await self.tasks.delete(user, eid)
        elif action_type == "create_event":
            await self.events.delete(user, eid)
        elif action_type == "complete_task":
            await self.tasks.update(user, eid, TaskUpdate(status=TaskStatus.open))
        else:
            raise HTTPException(status_code=400, detail="This action type cannot be undone yet")

        action["undone"] = True
        message.actions = list(message.actions)
        await self.db.commit()

    async def send(
        self, user: User, content: str, *, timezone_name: Optional[str] = None
    ) -> ChatMessage:
        await self._charge_if_hosted(user)
        api_key = self.user_service.resolve_gemini_key(user, self.settings)

        user_msg = ChatMessage(user_id=user.id, role=ChatRole.user, content=content.strip())
        self.db.add(user_msg)
        await self.db.commit()

        snapshot = await self._context_snapshot(user)
        history = await self.history(user, limit=12)
        prompt = f"{content.strip()}\n\n[Current schedule context]\n{snapshot}"
        contents = _build_contents(history, prompt)
        system_instruction = _system_prompt_with_clock(timezone_name)

        client = genai.Client(api_key=api_key)
        try:
            response = await client.aio.models.generate_content(
                model=self.settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            raw_text = _response_text(response)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Gemini error: {exc}") from exc

        cleaned, raw_actions = _extract_actions_block(raw_text)
        applied, linked = await self.apply_actions(
            user, raw_actions, timezone_name=timezone_name
        )

        assistant = ChatMessage(
            user_id=user.id,
            role=ChatRole.assistant,
            content=cleaned or "All set.",
            actions=applied or None,
            linked_entity_ids=linked or None,
        )
        self.db.add(assistant)
        await self.db.commit()
        await self.db.refresh(assistant)
        return assistant

    async def stream_tokens(
        self, user: User, content: str, *, timezone_name: Optional[str] = None
    ) -> AsyncIterator[str]:
        """Yield SSE-friendly chunks: token deltas, then a final actions payload."""
        await self._charge_if_hosted(user)
        api_key = self.user_service.resolve_gemini_key(user, self.settings)

        user_msg = ChatMessage(user_id=user.id, role=ChatRole.user, content=content.strip())
        self.db.add(user_msg)
        await self.db.commit()

        snapshot = await self._context_snapshot(user)
        history = await self.history(user, limit=12)
        prompt = f"{content.strip()}\n\n[Current schedule context]\n{snapshot}"
        contents = _build_contents(history, prompt)
        system_instruction = _system_prompt_with_clock(timezone_name)

        client = genai.Client(api_key=api_key)
        full_text = ""
        try:
            stream = await client.aio.models.generate_content_stream(
                model=self.settings.gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_instruction),
            )
            async for chunk in stream:
                piece = _response_text(chunk)
                if not piece:
                    continue
                full_text += piece
                yield json.dumps({"type": "token", "text": piece})
        except Exception as exc:
            yield json.dumps({"type": "error", "detail": str(exc)})
            return

        cleaned, raw_actions = _extract_actions_block(full_text)
        applied, linked = await self.apply_actions(
            user, raw_actions, timezone_name=timezone_name
        )
        assistant = ChatMessage(
            user_id=user.id,
            role=ChatRole.assistant,
            content=cleaned or "All set.",
            actions=applied or None,
            linked_entity_ids=linked or None,
        )
        self.db.add(assistant)
        await self.db.commit()
        await self.db.refresh(assistant)

        yield json.dumps(
            {
                "type": "done",
                "message": {
                    "id": str(assistant.id),
                    "role": assistant.role.value,
                    "content": assistant.content,
                    "actions": assistant.actions,
                    "linked_entity_ids": assistant.linked_entity_ids,
                    "created_at": assistant.created_at.isoformat(),
                },
            }
        )


def _parse_dt(value: Any, *, default_tz: Optional[ZoneInfo] = None) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

    tz = default_tz or timezone.utc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)

    # Models often stamp local wall times as Z/+00:00. If the user has a real
    # local timezone, reinterpret the clock face in that zone.
    offset = dt.utcoffset()
    tz_key = getattr(tz, "key", None)
    if (
        tz_key
        and tz_key != "UTC"
        and offset is not None
        and offset.total_seconds() == 0
    ):
        return dt.replace(tzinfo=None).replace(tzinfo=tz)
    return dt
