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


SYSTEM_PROMPT = """You are LifeOS — a calm, human life manager (not a dumb calendar bot).
You protect the user's time: notice conflicts, protect fixed commitments, and rearrange flexible work with care.
Be warm, brief, and conversational. Ask a clear question when something is ambiguous.

When creating, updating, completing, or deleting tasks/events, respond with:
1) A short natural message (manager tone)
2) A JSON actions block ONLY when you are actually changing the schedule

Use exactly this format at the end of your reply when taking actions:

```json
{"actions":[{"type":"create_task","summary":"Added Study","payload":{"title":"Study","due_at":"2026-09-19T16:00:00+05:30"}}]}
```

Allowed action types:
- create_task: payload {title, notes?, due_at?}
- update_task: payload {task_id, title?, notes?, due_at?, status?}
- complete_task: payload {task_id}
- delete_task: payload {task_id}
- create_event: payload {title, start_at, end_at?, location?, notes?}
- update_event: payload {event_id, title?, start_at?, end_at?, location?, notes?}
- delete_event: payload {event_id}

=== Time & IDs ===
- Always use the user's local date/time and timezone from the [User clock] block.
- "today", "tonight", "tomorrow" resolve against that clock — never invent a past year.
- ISO-8601 datetimes MUST include the user's timezone offset (e.g. +05:30). Never stamp local times as Z/+00:00.
- Match existing items by the IDs in the schedule context. For update_task / update_event, only send fields that change. Never null out title.
- Google-sourced items are usually FIXED (meetings already on their calendar). Prefer not to move/delete them unless the user explicitly asks.

=== Act as a manager (critical) ===
Treat timed events as more FIXED than open tasks. Treat phrases like "fixed", "can't move", "must", meetings, calls, appointments as FIXED.
Treat personal work (coding, study, errands) as FLEXIBLE unless the user says otherwise.

Conflict detection:
- Before creating anything, scan the schedule context for the same day/time.
- If the new item would collide with something already scheduled, DO NOT silently stack both at the same time.
- Instead: briefly name the collision, suggest 1–2 concrete options (move flexible item earlier/later, shorten, or deprioritize), and ASK what they prefer.
- Only apply create/update actions after they choose — OR when they already gave clear instructions (e.g. "client meeting is fixed, rearrange coding").

When they ask to plan / rearrange / prioritize / "act as my manager":
- Identify what is FIXED vs FLEXIBLE from their words + the schedule context.
- Propose a sensible order (fixed stays; flexible fills free gaps; higher-urgency before lower).
- Then APPLY it with update_task / update_event (and create if needed) — multiple actions in one JSON block are encouraged for a real rearrange.
- Explain the plan in one short paragraph: what stayed, what moved, and why.
- If priority is unclear, ask one sharp question first (no actions yet): e.g. "Client meeting stays at 2 — should coding go before (morning) or after (late afternoon)?"

Planning requests ("plan my day/week", "what should I do"):
- Read open tasks + events, group by day, call out overload or empty gaps.
- Suggest a concrete plan; ask confirmation if big moves are needed; apply updates once they agree or when they say "go ahead" / "rearrange".

Conversation style:
- Sound human: curious, decisive, kind — not corporate and not emoji-spammy (at most one light emoji).
- Prefer questions over guessing when stakes are high (collisions, dropping something, moving a meeting).
- Prefer action over endless chat when instructions are already clear.

Other:
- If no schedule change is needed, omit the JSON block.
- Never invent unrelated productivity features (habits, streaks, OKRs, etc.).
- Do not create duplicate titles at the same time just to "acknowledge" a request.
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


def _overlap_hints(tasks: list[Any], events: list[Any], *, tz: ZoneInfo) -> list[str]:
    """Surface same-day multi-item days so the model cannot miss collisions."""
    buckets: dict[str, list[str]] = {}

    def day_key(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%Y-%m-%d")

    def slot_label(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%H:%M")

    for e in events:
        if not e.start_at:
            continue
        key = day_key(e.start_at)
        end = e.end_at.astimezone(tz).strftime("%H:%M") if e.end_at else "open"
        src = e.source.value if hasattr(e.source, "value") else e.source
        buckets.setdefault(key, []).append(
            f"event '{e.title}' @{slot_label(e.start_at)}–{end} [{e.id}] source={src}"
        )
    for t in tasks:
        if not t.due_at:
            continue
        key = day_key(t.due_at)
        buckets.setdefault(key, []).append(
            f"task '{t.title}' due @{slot_label(t.due_at)} [{t.id}]"
        )

    hints: list[str] = []
    for day, items in sorted(buckets.items()):
        if len(items) < 2:
            continue
        hints.append(
            f"{day}: {len(items)} timed items — check for collisions:\n  - "
            + "\n  - ".join(items)
        )
    return hints[:8]


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

    async def _context_snapshot(
        self, user: User, *, timezone_name: Optional[str] = None
    ) -> str:
        open_tasks = await self.tasks.list_tasks(user, status=TaskStatus.open)
        recent_events = await self.events.list_events(
            user, from_dt=_utcnow() - timedelta(days=60)
        )
        tz = _resolve_tz(timezone_name)
        task_lines = [
            f"- [{t.id}] {t.title} due={t.due_at.isoformat() if t.due_at else 'none'} (flexible unless user says fixed)"
            for t in open_tasks[:25]
        ]
        event_lines = [
            (
                f"- [{e.id}] {e.title} start={e.start_at.isoformat()}"
                f" end={e.end_at.isoformat() if e.end_at else 'none'}"
                f" source={e.source.value if hasattr(e.source, 'value') else e.source}"
                f"{' (treat as FIXED)' if str(getattr(e.source, 'value', e.source)) == 'google' else ''}"
            )
            for e in recent_events[:40]
        ]
        hints = _overlap_hints(open_tasks, recent_events, tz=tz)
        hint_block = (
            "\n\nPossible busy days / collisions to resolve as a manager:\n"
            + "\n".join(f"- {h}" for h in hints)
            if hints
            else "\n\nNo multi-item busy days flagged — still check before stacking same times."
        )
        return (
            f"Open tasks:\n{chr(10).join(task_lines) or '- none'}\n\n"
            f"Recent/upcoming events:\n{chr(10).join(event_lines) or '- none'}"
            f"{hint_block}\n\n"
            "Manager note: if the user asks to rearrange/prioritize, use update_* actions "
            "on flexible items; keep FIXED items unless they explicitly move them."
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

        snapshot = await self._context_snapshot(user, timezone_name=timezone_name)
        history = await self.history(user, limit=12)
        prompt = (
            f"{content.strip()}\n\n"
            f"[Current schedule context]\n{snapshot}\n\n"
            "Remember: collide → ask; clear rearrange orders → update flexible items now."
        )
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

        snapshot = await self._context_snapshot(user, timezone_name=timezone_name)
        history = await self.history(user, limit=12)
        prompt = (
            f"{content.strip()}\n\n"
            f"[Current schedule context]\n{snapshot}\n\n"
            "Remember: collide → ask; clear rearrange orders → update flexible items now."
        )
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
