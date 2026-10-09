"""Allowlist and soft second-pass checks before schedule actions are applied."""

from __future__ import annotations

import re
from typing import Any

ALLOWED_ACTION_TYPES = frozenset(
    {
        "create_task",
        "update_task",
        "complete_task",
        "delete_task",
        "create_event",
        "update_event",
        "delete_event",
        "create_daily_week",
        "remember",
    }
)

_REQUIRED_PAYLOAD: dict[str, frozenset[str]] = {
    "create_task": frozenset({"title"}),
    "update_task": frozenset({"task_id"}),
    "complete_task": frozenset({"task_id"}),
    "delete_task": frozenset({"task_id"}),
    "create_event": frozenset({"title", "start_at"}),
    "update_event": frozenset({"event_id"}),
    "delete_event": frozenset({"event_id"}),
    "create_daily_week": frozenset({"title"}),
    "remember": frozenset({"note"}),
}

_ASKING_RE = re.compile(
    r"(?i)\b("
    r"should i|want me to|which (do you|would you)|"
    r"do you (want|prefer)|shall i|before i ("
    r"add|move|delete|change|create|rearrange"
    r")"
    r")\b"
)

_SCHEDULE_CHANGING = frozenset(ALLOWED_ACTION_TYPES - {"remember"})


def _has_required(action_type: str, payload: dict[str, Any]) -> bool:
    required = _REQUIRED_PAYLOAD.get(action_type)
    if required is None:
        return False
    for key in required:
        value = payload.get(key)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    if action_type == "create_daily_week":
        kind = str(payload.get("kind") or "task").lower()
        if kind == "event" and not payload.get("start_at"):
            return False
    return True


def looks_like_confirmation_question(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if "?" in cleaned and _ASKING_RE.search(cleaned):
        return True
    # Trailing question after proposing options is usually ask-first.
    if cleaned.endswith("?") and re.search(
        r"(?i)\b(or|option|prefer|before|after|morning|afternoon)\b", cleaned
    ):
        return True
    return False


def sanitize_actions(
    actions: list[dict[str, Any]],
    *,
    assistant_text: str = "",
) -> list[dict[str, Any]]:
    """Drop unknown types, incomplete payloads, and schedule edits while asking."""
    asking = looks_like_confirmation_question(assistant_text)
    cleaned: list[dict[str, Any]] = []
    for raw in actions:
        if not isinstance(raw, dict):
            continue
        action_type = str(raw.get("type") or "").strip()
        if action_type not in ALLOWED_ACTION_TYPES:
            continue
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        if not _has_required(action_type, payload):
            continue
        if asking and action_type in _SCHEDULE_CHANGING:
            continue
        cleaned.append(
            {
                "type": action_type,
                "summary": str(raw.get("summary") or ""),
                "payload": dict(payload),
            }
        )
    return cleaned
