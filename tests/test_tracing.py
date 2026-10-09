"""Conversation tracing stays off by default and records chat turns when enabled."""

import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.config import Settings
from app.core.tracing import (
    GenerationTrace,
    annotate_conversation,
    build_tracer_provider,
    console_export_enabled,
    conversation_span,
    finish_turn,
    langfuse_configured,
    langfuse_otlp,
    note_context,
    otlp_traces_endpoint,
    parse_otlp_headers,
    record_error,
    tracing_active,
)


def test_tracing_settings_default_off():
    fields = Settings.model_fields
    assert fields["otel_traces_enabled"].default is False
    assert fields["otel_service_name"].default == "lifeos-api"
    assert fields["otel_exporter_otlp_endpoint"].default == ""
    assert fields["otel_exporter_otlp_headers"].default == ""
    assert fields["otel_traces_console"].default is False
    assert fields["trace_capture_content"].default is False
    assert fields["langfuse_secret_key"].default == ""
    assert fields["langfuse_public_key"].default == ""
    assert fields["langfuse_base_url"].default == "https://us.cloud.langfuse.com"


def test_langfuse_keys_turn_tracing_on():
    missing_secret = SimpleNamespace(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="",
        langfuse_base_url="https://us.cloud.langfuse.com",
        otel_traces_enabled=False,
        otel_exporter_otlp_endpoint="",
        otel_traces_console=False,
    )
    assert langfuse_configured(missing_secret) is False
    assert tracing_active(missing_secret) is False
    assert langfuse_otlp(missing_secret) is None

    ready = SimpleNamespace(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_base_url="https://us.cloud.langfuse.com/",
        otel_traces_enabled=False,
        otel_exporter_otlp_endpoint="",
        otel_traces_console=False,
        is_dev=True,
    )
    assert tracing_active(ready) is True
    assert console_export_enabled(ready) is False
    ready.otel_traces_enabled = True
    assert console_export_enabled(ready) is False
    endpoint, headers = langfuse_otlp(ready)
    assert endpoint == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    decoded = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode()
    assert decoded == "pk-lf-test:sk-lf-test"
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_tracing_turns_on_from_endpoint_or_console():
    quiet = SimpleNamespace(
        otel_traces_enabled=False,
        otel_exporter_otlp_endpoint="",
        otel_traces_console=False,
    )
    assert tracing_active(quiet) is False
    assert tracing_active(SimpleNamespace(
        otel_traces_enabled=False,
        otel_exporter_otlp_endpoint="http://localhost:6006",
        otel_traces_console=False,
    ))
    assert console_export_enabled(SimpleNamespace(
        otel_traces_enabled=True,
        otel_exporter_otlp_endpoint="",
        otel_traces_console=False,
        is_dev=True,
    ))
    assert console_export_enabled(SimpleNamespace(
        otel_traces_enabled=True,
        otel_exporter_otlp_endpoint="http://localhost:4318",
        otel_traces_console=False,
        is_dev=True,
    )) is False


def test_otlp_endpoint_and_headers():
    assert otlp_traces_endpoint("http://localhost:6006") == "http://localhost:6006/v1/traces"
    assert (
        otlp_traces_endpoint("https://cloud.langfuse.com/api/public/otel/v1/traces/")
        == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    )
    headers = parse_otlp_headers("Authorization=Basic abc==, x-test=one=two")
    assert headers["Authorization"] == "Basic abc=="
    assert headers["x-test"] == "one=two"


def test_turn_span_hides_text_until_capture_is_on():
    provider, exporter = _memory_provider()
    settings = SimpleNamespace(gemini_model="gemini-2.0-flash", trace_capture_content=False)
    user = SimpleNamespace(id="user-1", ai_mode=SimpleNamespace(value="hosted"))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("lifeos.chat.send") as span:
        annotate_conversation(
            span,
            user=user,
            settings=settings,
            timezone_name="Asia/Kolkata",
            content="add gym at 6",
        )
        note_context(span, prompt="[First chat]\nhello", history_count=1)
        finish_turn(
            span,
            settings=settings,
            reply="Added gym",
            actions=[{"type": "create_event", "summary": "Added Gym"}],
            proposed=2,
        )
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["lifeos.user_id"] == "user-1"
    assert attrs["langfuse.user.id"] == "user-1"
    assert attrs["langfuse.session.id"] == "user-1"
    assert attrs["lifeos.ai_mode"] == "hosted"
    assert attrs["lifeos.input_chars"] == len("add gym at 6")
    assert attrs["lifeos.first_chat"] is True
    assert attrs["lifeos.actions.proposed"] == 2
    assert attrs["lifeos.actions.applied"] == 1
    assert attrs["lifeos.actions.dropped"] == 1
    assert attrs["lifeos.action_types"] == ("create_event",)
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs
    assert "langfuse.observation.input" not in attrs
    assert "lifeos.action_summaries" not in attrs
    provider.shutdown()


def test_turn_span_stores_conversation_when_capture_is_on():
    provider, exporter = _memory_provider()
    settings = SimpleNamespace(gemini_model="gemini-2.0-flash", trace_capture_content=True)
    user = SimpleNamespace(id="user-1", ai_mode=SimpleNamespace(value="byok"))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("lifeos.chat.send") as span:
        annotate_conversation(span, user=user, settings=settings, content="plan today")
        note_context(span, prompt="[Conversation gap]\ncheck in", history_count=4)
        finish_turn(
            span,
            settings=settings,
            reply="Want to move coding after 2?",
            actions=[{"type": "update_task", "summary": "Moved coding"}],
            proposed=1,
        )
    attrs = exporter.get_finished_spans()[0].attributes
    assert "plan today" in attrs["gen_ai.input.messages"]
    assert attrs["langfuse.observation.input"] == "plan today"
    assert "Want to move coding after 2?" in attrs["gen_ai.output.messages"]
    assert attrs["langfuse.observation.output"] == "Want to move coding after 2?"
    assert attrs["lifeos.gap_checkin"] is True
    assert attrs["lifeos.history_messages"] == 4
    assert attrs["lifeos.action_summaries"] == ("Moved coding",)
    provider.shutdown()


def test_model_span_records_token_usage():
    provider, exporter = _memory_provider()
    tracer = provider.get_tracer("test")
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(prompt_token_count=11, candidates_token_count=3),
        model_version="gemini-2.0-flash",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
    )
    with tracer.start_as_current_span("chat gemini-2.0-flash") as span:
        GenerationTrace(span).observe(response)
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.response.model"] == "gemini-2.0-flash"
    assert attrs["gen_ai.response.finish_reasons"] == ("STOP",)
    provider.shutdown()


def test_errors_redact_secrets_and_keep_status():
    provider, exporter = _memory_provider()
    tracer = provider.get_tracer("test")
    secret = "AIzaSyA1234567890123456789012"
    with tracer.start_as_current_span("lifeos.chat.send") as span:
        record_error(span, RuntimeError(f"request failed key={secret} token={secret}"))
    finished = exporter.get_finished_spans()[0]
    assert finished.status.status_code == StatusCode.ERROR
    assert secret not in (finished.status.description or "")
    event = finished.events[0].attributes
    assert secret not in event["exception.message"]
    assert secret not in event["exception.stacktrace"]
    provider.shutdown()

    provider, exporter = _memory_provider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("lifeos.chat.send") as span:
        record_error(span, HTTPException(status_code=402, detail="Not enough AI credits"))
    finished = exporter.get_finished_spans()[0]
    assert finished.attributes["http.response.status_code"] == 402
    assert finished.attributes["error.type"] == "HTTPException"
    assert "Not enough AI credits" in (finished.status.description or "")
    provider.shutdown()


def test_conversation_span_reraises_without_an_exporter():
    user = SimpleNamespace(id="user-1", ai_mode=SimpleNamespace(value="hosted"))
    settings = SimpleNamespace(gemini_model="gemini-2.0-flash", trace_capture_content=False)
    with pytest.raises(RuntimeError, match="boom"):
        with conversation_span(
            "lifeos.chat.send",
            user=user,
            settings=settings,
            content="hello",
        ):
            raise RuntimeError("boom")


def test_console_provider_builds_without_starting_otlp():
    settings = SimpleNamespace(
        otel_traces_enabled=True,
        otel_traces_console=True,
        otel_exporter_otlp_endpoint="",
        otel_exporter_otlp_headers="",
        otel_service_name="lifeos-api",
        app_name="LifeOS",
        app_env="development",
        is_dev=True,
    )
    provider = build_tracer_provider(settings)
    assert provider is not None
    provider.shutdown()


def _memory_provider():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter
