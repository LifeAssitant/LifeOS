"""Regression checks for chat guardrails, gaps, and prompt fixtures."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.action_guard import looks_like_confirmation_question, sanitize_actions
from app.services.gemini import SYSTEM_PROMPT, TURN_REMINDER, GeminiChatService


FIXTURES = Path(__file__).parent / "fixtures" / "chat_eval_cases.json"


def test_system_prompt_has_scope_and_ask_vs_act():
    assert "OUT OF SCOPE" in SYSTEM_PROMPT
    assert "Ask vs act" in SYSTEM_PROMPT
    assert "remember" in SYSTEM_PROMPT
    assert "off-topic" in TURN_REMINDER


def test_allowlist_drops_unknown_and_incomplete():
    actions = [
        {"type": "hack_system", "payload": {}},
        {"type": "create_task", "payload": {}},
        {"type": "create_task", "payload": {"title": "Study"}, "summary": "Added Study"},
        {"type": "complete_task", "payload": {"task_id": "abc"}},
        {"type": "remember", "payload": {"note": "don't move mornings"}},
    ]
    cleaned = sanitize_actions(actions)
    types = [a["type"] for a in cleaned]
    assert types == ["create_task", "complete_task", "remember"]


def test_asking_blocks_schedule_changes_keeps_remember():
    text = "Client meeting stays at 2 — should coding go before or after?"
    assert looks_like_confirmation_question(text)
    actions = [
        {
            "type": "update_task",
            "payload": {"task_id": "11111111-1111-1111-1111-111111111111"},
            "summary": "Moved coding",
        },
        {"type": "remember", "payload": {"note": "client meeting is fixed at 2"}},
    ]
    cleaned = sanitize_actions(actions, assistant_text=text)
    assert [a["type"] for a in cleaned] == ["remember"]


def test_gap_prompt_blocks():
    class S:
        chat_check_in_hours = 5.5

    svc = GeminiChatService.__new__(GeminiChatService)
    svc.settings = S()
    assert "[First chat]" in svc._gap_prompt_block(None)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    assert svc._gap_prompt_block(recent) == ""
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    assert "[Conversation gap]" in svc._gap_prompt_block(old)


def test_eval_fixtures_cover_core_intents():
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    ids = {c["id"] for c in cases}
    assert {
        "plan_day",
        "collision",
        "off_topic",
        "daily_routine",
        "clear_create",
        "remember_pref",
    } <= ids
    for case in cases:
        assert case["user"].strip()
        assert case["expect"] in {
            "ask_or_plan",
            "ask_no_actions",
            "redirect_no_actions",
            "may_ask_daily",
            "create_action",
            "remember_or_note",
        }


def test_fixture_off_topic_matches_prompt_scope():
    cases = {c["id"]: c for c in json.loads(FIXTURES.read_text(encoding="utf-8"))}
    assert "coding" in cases["off_topic"]["user"].lower() or "debug" in cases["off_topic"]["user"].lower()
    assert "OUT OF SCOPE" in SYSTEM_PROMPT
