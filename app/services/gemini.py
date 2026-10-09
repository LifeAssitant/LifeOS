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
from app.core.tracing import (
    actions_span,
    conversation_span,
    finish_turn,
    model_span,
    note_context,
    record_error,
)
from app.models import AIMode, ChatMessage, ChatRole, TaskSource, TaskStatus, User
from app.schemas import EventCreate, EventUpdate, TaskCreate, TaskUpdate
from app.services.action_guard import sanitize_actions
from app.services.lifecycle import EventService, TaskService, UserService

MAX_AI_MEMORY = 20
_PREF_PATTERNS = (
    re.compile(r"(?i)\bdon'?t move (my )?mornings?\b"),
    re.compile(r"(?i)\b(no|never) (move|schedule).{0,24}\b(mornings?|evenings?|nights?)\b"),
    re.compile(r"(?i)\bi (usually|always|prefer to) work\b.{0,40}"),
    re.compile(r"(?i)\b(busy|blocked|free) (before|after|until|from)\b.{0,30}"),
    re.compile(r"(?i)\bkeep (mornings?|evenings?|afternoons?) (free|clear|open)\b"),
    re.compile(r"(?i)\bprefer(s|ences?)?:?.{0,60}"),
)


SYSTEM_PROMPT = """You are LifeOS — a warm, human life manager sitting with the user (not a stiff calendar bot).
Your only job is helping them talk through, plan, and manage their time: tasks, calendar, priorities, routines, reminders, and rearranging the day or week.
Protect their time: notice conflicts, keep fixed commitments, and move flexible work with care.

=== Scope (hard) ===
IN SCOPE: planning, tasks, events, priorities, routines, reminders, focus blocks, "what should I do", rearranging the day/week, checking how the day is going as it relates to the plan.
OUT OF SCOPE: homework answers, coding/debugging help, medical or legal advice, general trivia, essays, therapy-as-treatment, roleplay, or anything unrelated to managing their life schedule.
If they go off-topic: one short, kind redirect back to planning (e.g. "I stay on plans and calendars — want to put a study block on today instead?"). Never emit a JSON actions block for off-scope asks.

=== Ask vs act (clear intention) ===
Ask first (no actions yet) when:
- time/priority is ambiguous, or two options are equally plausible
- a new item would collide with something already scheduled
- moving/deleting a FIXED item (meetings, "can't move", appointments)
- filling a possible everyday routine they have not confirmed as daily

Act now with JSON actions when:
- they gave a clear instruction ("add gym at 6", "mark study done", "move coding after the 2pm meeting")
- they confirmed a choice you offered ("go ahead", "yes every day", "put it after")
- they asked to rearrange/prioritize and FIXED vs FLEXIBLE is clear from their words + schedule context

When creating, updating, completing, or deleting tasks/events, respond with:
1) A short natural message (manager tone)
2) A JSON actions block ONLY when you are actually changing the schedule

Use exactly this format at the end of your reply when taking actions:

```json
{"actions":[{"type":"create_task","summary":"Added Study","payload":{"title":"Study","due_at":"2026-09-19T16:00:00+05:30"}}]}
```

Everyday for a week (one action, not seven):

```json
{"actions":[{"type":"create_daily_week","summary":"Added Gym every day this week","payload":{"kind":"event","title":"Gym","start_at":"2026-09-19T18:00:00+05:30","end_at":"2026-09-19T19:00:00+05:30","days":7}}]}
```

Allowed action types:
- create_task: payload {title, notes?, due_at?}
- update_task: payload {task_id, title?, notes?, due_at?, status?}
- complete_task: payload {task_id}
- delete_task: payload {task_id}
- create_event: payload {title, start_at, end_at?, location?, notes?}
- update_event: payload {event_id, title?, start_at?, end_at?, location?, notes?}
- delete_event: payload {event_id}
- create_daily_week: payload {kind:"task"|"event", title, notes?, location?, due_at?, start_at?, end_at?, days?:7}
  One action fills the same item across consecutive days (default 7). Never emit 7 separate creates.
- remember: payload {note} — durable planning preference (e.g. "don't move mornings", "works best after 10am").
  Use when they state a lasting preference. Never invent preferences. Never emit remember while also asking a confirmation question.

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

=== Everyday items (fill the next week) ===
Some things are one-offs. Some are things the user might do every day. Notice the difference.

Treat as likely everyday when they mention a routine-shaped activity: gym, workout, walk, run, commute, class, lecture, medication, prayer, meditation, study block, practice, standup, "I usually…", "I always…", morning/evening rituals.
Treat as one-off when they pin a single date ("tomorrow", "this Friday"), name a unique appointment, or talk about a meeting/call that is not a repeating class.

Then:
1. Already known as daily — they said "every day" / "daily" / "each day" / "all week" / "the whole week", OR earlier in this chat they confirmed it is daily:
   Use create_daily_week now. Do not ask again.
   kind=task for todos (due_at = first day's time). kind=event for timed blocks (start_at + end_at of the first occurrence; duration is copied).
   Start from the day they named, or today. days defaults to 7. Backend skips a day if that title already exists there.
2. Might be daily, but they have not said so:
   Create only the mentioned day (or today) with create_task / create_event.
   Ask one short question, e.g. "Do you do this every day? I can put it on the next 7 days."
   No create_daily_week yet.
3. They answer yes / every day / this week:
   Use create_daily_week for 7 days from the original time. Backend skips the day already created.
4. Clearly one-off: never ask, never fill the week.

Before filling a week, glance at the schedule. If that time is already packed on most days, say so and ask — otherwise just fill it.

=== Conversation style ===
- Sound human: curious, decisive, kind — not corporate and not emoji-spammy (at most one light emoji).
- Keep replies short. Prefer one sharp question over a list of questions.
- Prefer action over endless chat when instructions are already clear.
- If a [Conversation gap] block is present, open with a brief human check-in about their day/plan (use schedule context), then help with what they just said. One question + one useful next step — no lecture.
- If a [First chat] block is present, welcome lightly and ask what to put on the calendar.

Other:
- If no schedule change is needed, omit the JSON block.
- Do not invent habit streaks, gamification, OKRs, or other extra product features.
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


def _hours_since(earlier: datetime, later: datetime) -> float:
    if earlier.tzinfo is None:
        earlier = earlier.replace(tzinfo=timezone.utc)
    if later.tzinfo is None:
        later = later.replace(tzinfo=timezone.utc)
    return max(0.0, (later - earlier).total_seconds() / 3600.0)


TURN_REMINDER = (
    "Remember: stay on planning/managing only; collide or unclear → ask (no actions); "
    "clear rearrange/create/complete orders → act now; everyday routine → ask or fill the next 7 days; "
    "off-topic → brief redirect, no JSON."
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


def _local_day_key(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def _shift_local_days(dt: datetime, days: int, tz: ZoneInfo) -> datetime:
    local = dt.astimezone(tz)
    target = local.date() + timedelta(days=days)
    return datetime.combine(target, local.time(), tzinfo=tz)


def _is_week_repeat(payload: dict[str, Any]) -> bool:
    repeat = str(
        payload.get("repeat") or payload.get("recurrence") or payload.get("every") or ""
    ).lower()
    if repeat in {"daily", "every_day", "everyday", "every day", "week", "weekly"}:
        return True
    try:
        days = int(payload.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    return days >= 3


def _default_local_morning(tz: ZoneInfo) -> datetime:
    now = datetime.now(tz)
    return now.replace(hour=9, minute=0, second=0, microsecond=0)


def _fold_repeated_creates(applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If the model emitted several same-title creates, show one undo chip."""
    folded: list[dict[str, Any]] = []
    i = 0
    while i < len(applied):
        cur = applied[i]
        action_type = cur.get("type")
        if action_type not in {"create_task", "create_event"}:
            folded.append(cur)
            i += 1
            continue
        title = ((cur.get("payload") or {}).get("title") or "").strip().casefold()
        group = [cur]
        j = i + 1
        while j < len(applied) and applied[j].get("type") == action_type:
            other = ((applied[j].get("payload") or {}).get("title") or "").strip().casefold()
            if not title or other != title:
                break
            group.append(applied[j])
            j += 1
        if len(group) >= 3:
            ids = [g["entity_id"] for g in group if g.get("entity_id")]
            kind = "task" if action_type == "create_task" else "event"
            label = (cur.get("payload") or {}).get("title") or "this"
            folded.append(
                {
                    "type": "create_daily_week",
                    "summary": f"Added {label} every day this week",
                    "payload": {**(cur.get("payload") or {}), "kind": kind, "days": len(group)},
                    "entity_id": ids[0] if ids else None,
                    "entity_ids": ids,
                    "kind": kind,
                    "undoable": True,
                }
            )
        else:
            folded.extend(group)
        i = j
    return folded


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

    async def _previous_user_message_at(
        self, user: User, *, exclude_id: Optional[UUID] = None
    ) -> Optional[datetime]:
        """Timestamp of the latest prior user message (excluding the one just saved)."""
        stmt = (
            select(ChatMessage.created_at)
            .where(
                ChatMessage.user_id == user.id,
                ChatMessage.role == ChatRole.user,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(ChatMessage.id != exclude_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    def _gap_prompt_block(self, previous_user_at: Optional[datetime]) -> str:
        threshold_h = float(self.settings.chat_check_in_hours)
        if previous_user_at is None:
            return (
                "[First chat]\n"
                "- This is their first message with you.\n"
                "- Welcome lightly in one short line, then ask what to put on the calendar "
                "or what today should look like. Help with their message after that.\n"
            )
        hours = _hours_since(previous_user_at, _utcnow())
        if hours < threshold_h:
            return ""
        rounded = round(hours, 1)
        return (
            "[Conversation gap]\n"
            f"- It has been about {rounded} hours since they last messaged you "
            f"(threshold {threshold_h}h).\n"
            "- Open with one brief human check-in about their day or plan "
            "(use the schedule context). Then help with what they just said.\n"
            "- One question + one useful next step. No lecture, no emoji spam.\n"
        )

    def _build_user_prompt(
        self,
        content: str,
        snapshot: str,
        *,
        previous_user_at: Optional[datetime],
    ) -> str:
        gap = self._gap_prompt_block(previous_user_at)
        gap_section = f"{gap}\n" if gap else ""
        return (
            f"{content.strip()}\n\n"
            f"[Current schedule context]\n{snapshot}\n\n"
            f"{gap_section}"
            f"{TURN_REMINDER}"
        )

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

    def _memory_lines(self, user: User) -> list[str]:
        raw = getattr(user, "ai_memory", None) or []
        if not isinstance(raw, list):
            return []
        lines: list[str] = []
        for item in raw[:MAX_AI_MEMORY]:
            if isinstance(item, str) and item.strip():
                lines.append(item.strip())
            elif isinstance(item, dict):
                note = str(item.get("note") or item.get("text") or "").strip()
                if note:
                    lines.append(note)
        return lines

    async def _remember_note(self, user: User, note: str) -> str:
        cleaned = " ".join((note or "").strip().split())
        if not cleaned:
            raise ValueError("empty preference")
        if len(cleaned) > 180:
            cleaned = cleaned[:177] + "…"
        memory = list(self._memory_lines(user))
        key = cleaned.casefold()
        memory = [m for m in memory if m.casefold() != key]
        memory.append(cleaned)
        user.ai_memory = memory[-MAX_AI_MEMORY:]
        await self.db.commit()
        await self.db.refresh(user)
        return cleaned

    async def _ingest_user_preferences(self, user: User, content: str) -> None:
        text = (content or "").strip()
        if not text:
            return
        for pattern in _PREF_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            note = match.group(0).strip(" .,:;")
            if len(note) < 8:
                continue
            try:
                await self._remember_note(user, note)
            except Exception:
                await self.db.rollback()
            break

    async def day_load(
        self, user: User, *, timezone_name: Optional[str] = None
    ) -> tuple[int, int]:
        """Open tasks overall and events starting today (for check-in copy)."""
        tz = _resolve_tz(timezone_name)
        local_now = datetime.now(tz)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        tasks = await self.tasks.list_tasks(user, status=TaskStatus.open)
        events = await self.events.list_events(user, from_dt=day_start, to_dt=day_end)
        return len(tasks), len(events)

    async def soft_greeting(
        self, user: User, *, timezone_name: Optional[str] = None
    ) -> dict[str, Any]:
        """Template check-in when chat opens after a gap — no Gemini, no credit charge."""
        previous = await self._previous_user_message_at(user)
        threshold_h = float(self.settings.chat_check_in_hours)
        if previous is None:
            return {
                "show": True,
                "kind": "first",
                "hours_since": None,
                "message": "Hey — what should we get on the calendar first?",
            }
        hours = _hours_since(previous, _utcnow())
        if hours < threshold_h:
            return {"show": False, "kind": None, "hours_since": hours, "message": None}

        task_n, event_n = await self.day_load(user, timezone_name=timezone_name)
        if task_n or event_n:
            bits: list[str] = []
            if task_n:
                bits.append(f"{task_n} open task{'s' if task_n != 1 else ''}")
            if event_n:
                bits.append(f"{event_n} event{'s' if event_n != 1 else ''}")
            load = " and ".join(bits)
            message = f"Welcome back — today looks like {load}. Want to plan the next few hours?"
        else:
            message = "Welcome back — today’s pretty open. What should we line up?"
        return {
            "show": True,
            "kind": "gap",
            "hours_since": round(hours, 1),
            "message": message,
        }

    async def _context_snapshot(
        self, user: User, *, timezone_name: Optional[str] = None
    ) -> str:
        tz = _resolve_tz(timezone_name)
        local_now = datetime.now(tz)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = day_start - timedelta(days=1)
        window_end = day_start + timedelta(days=14)

        open_tasks = await self.tasks.list_tasks(
            user,
            status=TaskStatus.open,
            from_dt=window_start,
            to_dt=window_end,
        )
        recent_events = await self.events.list_events(
            user, from_dt=window_start, to_dt=window_end
        )
        # Keep context small when the horizon is quiet.
        task_cap = 8 if len(open_tasks) <= 3 and len(recent_events) <= 2 else 15
        event_cap = 8 if len(recent_events) <= 2 else 24

        task_lines = [
            f"- [{t.id}] {t.title} due={t.due_at.isoformat() if t.due_at else 'none'} (flexible unless user says fixed)"
            for t in open_tasks[:task_cap]
        ]
        event_lines = [
            (
                f"- [{e.id}] {e.title} start={e.start_at.isoformat()}"
                f" end={e.end_at.isoformat() if e.end_at else 'none'}"
                f" source={e.source.value if hasattr(e.source, 'value') else e.source}"
                f"{' (treat as FIXED)' if str(getattr(e.source, 'value', e.source)) == 'google' else ''}"
            )
            for e in recent_events[:event_cap]
        ]
        hints = _overlap_hints(open_tasks[:task_cap], recent_events[:event_cap], tz=tz)
        if open_tasks or recent_events:
            hint_block = (
                "\n\nPossible busy days / collisions to resolve as a manager:\n"
                + "\n".join(f"- {h}" for h in hints)
                if hints
                else "\n\nNo multi-item busy days flagged — still check before stacking same times."
            )
        else:
            hint_block = "\n\nSchedule looks light in the next two weeks — keep suggestions small."

        memory = self._memory_lines(user)
        memory_block = (
            "\n\nKnown planning preferences (honor these):\n"
            + "\n".join(f"- {m}" for m in memory)
            if memory
            else "\n\nKnown planning preferences: none saved yet."
        )
        return (
            f"Open tasks (yesterday → +14 days):\n{chr(10).join(task_lines) or '- none'}\n\n"
            f"Events (yesterday → +14 days):\n{chr(10).join(event_lines) or '- none'}"
            f"{hint_block}"
            f"{memory_block}\n\n"
            "Manager note: if the user asks to rearrange/prioritize, use update_* actions "
            "on flexible items; keep FIXED items unless they explicitly move them.\n"
            "Everyday note: if a new item sounds like a daily routine and they have not "
            "said so, ask before filling the week. If they already said daily / every day, "
            "use create_daily_week (7 consecutive days)."
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

    async def _occupied_title_days(
        self, user: User, title: str, tz: ZoneInfo
    ) -> set[str]:
        needle = title.strip().casefold()
        occupied: set[str] = set()
        if not needle:
            return occupied
        window_start = _utcnow() - timedelta(days=1)
        window_end = _utcnow() + timedelta(days=10)
        tasks = await self.tasks.list_tasks(
            user, status=TaskStatus.open, from_dt=window_start, to_dt=window_end
        )
        events = await self.events.list_events(
            user, from_dt=window_start, to_dt=window_end
        )
        for task in tasks:
            if task.title.strip().casefold() != needle or not task.due_at:
                continue
            occupied.add(_local_day_key(task.due_at, tz))
        for event in events:
            if event.title.strip().casefold() != needle:
                continue
            occupied.add(_local_day_key(event.start_at, tz))
        return occupied

    async def _create_daily_week(
        self,
        user: User,
        payload: dict[str, Any],
        *,
        tz: ZoneInfo,
    ) -> tuple[str, list[str], str]:
        kind = str(payload.get("kind") or "task").strip().lower()
        if kind not in {"task", "event"}:
            kind = "event" if payload.get("start_at") else "task"
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("daily week needs a title")

        try:
            days = int(payload.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 7))

        first = _parse_dt(
            payload.get("due_at") or payload.get("start_at"), default_tz=tz
        ) or _default_local_morning(tz)
        end_at = _parse_dt(payload.get("end_at"), default_tz=tz)
        duration = (end_at - first) if end_at and end_at > first else timedelta(hours=1)

        occupied = await self._occupied_title_days(user, title, tz)
        for raw_skip in payload.get("skip_dates") or []:
            occupied.add(str(raw_skip)[:10])

        created_ids: list[str] = []
        for offset in range(days):
            when = _shift_local_days(first, offset, tz)
            day_key = _local_day_key(when, tz)
            if day_key in occupied:
                continue
            if kind == "event":
                event = await self.events.create(
                    user,
                    EventCreate(
                        title=title,
                        notes=payload.get("notes"),
                        location=payload.get("location"),
                        start_at=when,
                        end_at=when + duration,
                        source=TaskSource.chat,
                    ),
                )
                created_ids.append(str(event.id))
            else:
                task = await self.tasks.create(
                    user,
                    TaskCreate(
                        title=title,
                        notes=payload.get("notes"),
                        due_at=when,
                        source=TaskSource.chat,
                    ),
                )
                created_ids.append(str(task.id))
            occupied.add(day_key)

        count = len(created_ids)
        if count == 0:
            raise ValueError("daily week created nothing new")
        if count == 1:
            summary = f"Added “{title}”"
        else:
            summary = f"Added “{title}” every day this week ({count} days)"
        return kind, created_ids, summary

    async def apply_actions(
        self,
        user: User,
        actions: list[dict[str, Any]],
        *,
        timezone_name: Optional[str] = None,
        assistant_text: str = "",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        applied: list[dict[str, Any]] = []
        linked: list[str] = []
        default_tz = _resolve_tz(timezone_name)
        safe_actions = sanitize_actions(actions, assistant_text=assistant_text)

        for raw in safe_actions:
            action_type = raw.get("type")
            payload = dict(raw.get("payload") or {})
            summary = raw.get("summary") or ""
            entity_id: Optional[str] = None
            entity_ids: list[str] = []
            week_kind: Optional[str] = None

            original_type = action_type
            if action_type in {"create_task", "create_event"} and _is_week_repeat(payload):
                action_type = "create_daily_week"
                payload["kind"] = "event" if original_type == "create_event" else "task"

            try:
                if action_type == "remember":
                    note = await self._remember_note(user, str(payload.get("note") or ""))
                    summary = summary or f"Remembered: {note}"
                elif action_type == "create_daily_week":
                    week_kind, entity_ids, week_summary = await self._create_daily_week(
                        user, payload, tz=default_tz
                    )
                    entity_id = entity_ids[0] if entity_ids else None
                    summary = summary or week_summary
                elif action_type == "create_task":
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
                "entity_ids": entity_ids or ([entity_id] if entity_id else []),
                "kind": week_kind,
                "undoable": action_type
                in {
                    "create_task",
                    "create_event",
                    "create_daily_week",
                    "complete_task",
                },
                "changed_schedule": action_type
                not in {"remember"},
            }
            applied.append(record)
            if entity_ids:
                linked.extend(entity_ids)
            elif entity_id:
                linked.append(entity_id)

        return _fold_repeated_creates(applied), linked

    async def undo_action(self, user: User, message_id: UUID, action_index: int) -> None:
        with conversation_span("lifeos.chat.undo", user=user, settings=self.settings) as span:
            span.set_attribute("lifeos.message_id", str(message_id))
            span.set_attribute("lifeos.action_index", int(action_index))
            await self._undo_action(user, message_id, action_index, span)

    async def _undo_action(
        self, user: User, message_id: UUID, action_index: int, span: Any
    ) -> None:
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
        span.set_attribute("lifeos.action_type", str(action.get("type") or ""))
        action_type = action.get("type")
        entity_ids = [
            UUID(str(item))
            for item in (action.get("entity_ids") or [])
            if item
        ]
        if not entity_ids and action.get("entity_id"):
            entity_ids = [UUID(str(action["entity_id"]))]
        if not entity_ids:
            raise HTTPException(status_code=400, detail="Action cannot be undone")

        kind = str(action.get("kind") or (action.get("payload") or {}).get("kind") or "")
        if action_type == "create_daily_week":
            for eid in entity_ids:
                try:
                    if kind == "event":
                        await self.events.delete(user, eid)
                    else:
                        await self.tasks.delete(user, eid)
                except Exception:
                    await self.db.rollback()
                    try:
                        if kind == "event":
                            await self.tasks.delete(user, eid)
                        else:
                            await self.events.delete(user, eid)
                    except Exception:
                        await self.db.rollback()
        elif action_type == "create_task":
            await self.tasks.delete(user, entity_ids[0])
        elif action_type == "create_event":
            await self.events.delete(user, entity_ids[0])
        elif action_type == "complete_task":
            await self.tasks.update(user, entity_ids[0], TaskUpdate(status=TaskStatus.open))
        else:
            raise HTTPException(status_code=400, detail="This action type cannot be undone yet")

        action["undone"] = True
        message.actions = list(message.actions)
        await self.db.commit()

    async def send(
        self, user: User, content: str, *, timezone_name: Optional[str] = None
    ) -> ChatMessage:
        with conversation_span(
            "lifeos.chat.send",
            user=user,
            settings=self.settings,
            timezone_name=timezone_name,
            content=content,
        ) as span:
            await self._charge_if_hosted(user)
            api_key = self.user_service.resolve_gemini_key(user, self.settings)

            user_msg = ChatMessage(user_id=user.id, role=ChatRole.user, content=content.strip())
            self.db.add(user_msg)
            await self.db.commit()
            await self.db.refresh(user_msg)

            await self._ingest_user_preferences(user, content)
            previous_user_at = await self._previous_user_message_at(
                user, exclude_id=user_msg.id
            )
            snapshot = await self._context_snapshot(user, timezone_name=timezone_name)
            history = await self.history(user, limit=12)
            prompt = self._build_user_prompt(
                content, snapshot, previous_user_at=previous_user_at
            )
            note_context(span, prompt=prompt, history_count=len(history))
            contents = _build_contents(history, prompt)
            system_instruction = _system_prompt_with_clock(timezone_name)

            client = genai.Client(api_key=api_key)
            with model_span(self.settings) as generation:
                try:
                    response = await client.aio.models.generate_content(
                        model=self.settings.gemini_model,
                        contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system_instruction),
                    )
                except Exception as exc:
                    raise HTTPException(status_code=502, detail=f"Gemini error: {exc}") from exc
                generation.observe(response)
                raw_text = _response_text(response)

            cleaned, raw_actions = _extract_actions_block(raw_text)
            with actions_span(len(raw_actions)):
                applied, linked = await self.apply_actions(
                    user,
                    raw_actions,
                    timezone_name=timezone_name,
                    assistant_text=cleaned,
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
            finish_turn(
                span,
                settings=self.settings,
                reply=assistant.content,
                actions=applied,
                proposed=len(raw_actions),
            )
            return assistant

    async def stream_tokens(
        self, user: User, content: str, *, timezone_name: Optional[str] = None
    ) -> AsyncIterator[str]:
        """Yield SSE-friendly chunks: token deltas, then a final actions payload."""
        with conversation_span(
            "lifeos.chat.stream",
            user=user,
            settings=self.settings,
            timezone_name=timezone_name,
            content=content,
        ) as span:
            await self._charge_if_hosted(user)
            api_key = self.user_service.resolve_gemini_key(user, self.settings)

            user_msg = ChatMessage(user_id=user.id, role=ChatRole.user, content=content.strip())
            self.db.add(user_msg)
            await self.db.commit()
            await self.db.refresh(user_msg)

            await self._ingest_user_preferences(user, content)
            previous_user_at = await self._previous_user_message_at(
                user, exclude_id=user_msg.id
            )
            snapshot = await self._context_snapshot(user, timezone_name=timezone_name)
            history = await self.history(user, limit=12)
            prompt = self._build_user_prompt(
                content, snapshot, previous_user_at=previous_user_at
            )
            note_context(span, prompt=prompt, history_count=len(history))
            contents = _build_contents(history, prompt)
            system_instruction = _system_prompt_with_clock(timezone_name)

            client = genai.Client(api_key=api_key)
            full_text = ""
            with model_span(self.settings) as generation:
                try:
                    stream = await client.aio.models.generate_content_stream(
                        model=self.settings.gemini_model,
                        contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system_instruction),
                    )
                    async for chunk in stream:
                        generation.observe(chunk)
                        piece = _response_text(chunk)
                        if not piece:
                            continue
                        full_text += piece
                        yield json.dumps({"type": "token", "text": piece})
                except Exception as exc:
                    record_error(generation.span, exc)
                    record_error(span, exc)
                    yield json.dumps({"type": "error", "detail": str(exc)})
                    return

            cleaned, raw_actions = _extract_actions_block(full_text)
            with actions_span(len(raw_actions)):
                applied, linked = await self.apply_actions(
                    user,
                    raw_actions,
                    timezone_name=timezone_name,
                    assistant_text=cleaned,
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
            finish_turn(
                span,
                settings=self.settings,
                reply=assistant.content,
                actions=applied,
                proposed=len(raw_actions),
            )

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

    async def transcribe_audio(
        self,
        user: User,
        audio: bytes,
        mime_type: str,
    ) -> str:
        with conversation_span(
            "lifeos.chat.transcribe",
            user=user,
            settings=self.settings,
        ) as span:
            if user.ai_mode == AIMode.hosted:
                enforce_hosted_chat_limit(user.id)
            api_key = self.user_service.resolve_gemini_key(user, self.settings)
            mime = _normalize_audio_mime(mime_type)
            span.set_attribute("lifeos.audio_bytes", len(audio))
            span.set_attribute("lifeos.audio_mime", mime)
            client = genai.Client(api_key=api_key)
            with model_span(self.settings) as generation:
                try:
                    response = await client.aio.models.generate_content(
                        model=self.settings.gemini_model,
                        contents=[
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_bytes(data=audio, mime_type=mime),
                                    types.Part.from_text(
                                        text=(
                                            "Transcribe this voice note. Return only the spoken words. "
                                            "No quotes, labels, or commentary. "
                                            "If there is no speech, return an empty string."
                                        )
                                    ),
                                ],
                            )
                        ],
                        config=types.GenerateContentConfig(temperature=0),
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=502, detail=f"Could not hear that: {exc}"
                    ) from exc
                generation.observe(response)
            text = _clean_transcript(_response_text(response))
            span.set_attribute("lifeos.transcript_chars", len(text))
            if self.settings.trace_capture_content and text:
                finish_turn(
                    span,
                    settings=self.settings,
                    reply=text,
                    actions=[],
                    proposed=0,
                )
            return text


_AUDIO_MIME_ALIASES = {
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/aac",
    "audio/mpeg": "audio/mp3",
    "audio/mp3": "audio/mp3",
    "audio/wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/webm": "audio/webm",
    "video/webm": "audio/webm",
    "audio/ogg": "audio/ogg",
    "audio/opus": "audio/ogg",
    "audio/flac": "audio/flac",
    "audio/3gpp": "audio/3gpp",
    "audio/amr": "audio/amr",
    "audio/x-caf": "audio/aac",
}


def _normalize_audio_mime(value: str) -> str:
    raw = (value or "").split(";")[0].strip().lower()
    return _AUDIO_MIME_ALIASES.get(raw, raw or "audio/webm")


def _clean_transcript(text: str) -> str:
    cleaned = (text or "").strip().strip('"').strip("'")
    lowered = cleaned.lower()
    if lowered in {"", "empty", "(empty)", "[empty]", "no speech", "inaudible"}:
        return ""
    return cleaned


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
