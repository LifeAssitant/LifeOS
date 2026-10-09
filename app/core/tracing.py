"""OpenTelemetry traces for LifeOS conversations.

Each chat turn (send, stream, transcription, undo) becomes a span. Model calls
are child spans using GenAI semantic conventions, so an OTLP backend such as
Phoenix, Langfuse, Jaeger, Tempo, or Honeycomb can show the conversation.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import traceback
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.config import Settings

logger = logging.getLogger(__name__)

_CONTENT_LIMIT = 4000
_ERROR_LIMIT = 500
_SECRET_PATTERNS = (
    re.compile(r"(?i)(key=)[^&\s]+"),
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),
)

_installed = False


_LANGFUSE_BAGGAGE = (
    "langfuse.user.id",
    "langfuse.session.id",
    "langfuse.trace.name",
)


def _setting(settings: Any, name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def langfuse_configured(settings: Any) -> bool:
    return bool(_setting(settings, "langfuse_public_key") and _setting(settings, "langfuse_secret_key"))


def langfuse_otlp(settings: Any) -> Optional[tuple[str, dict[str, str]]]:
    """OTLP endpoint and headers for the Langfuse project in the env."""
    public = _setting(settings, "langfuse_public_key")
    secret = _setting(settings, "langfuse_secret_key")
    if not public or not secret:
        return None
    base = _setting(settings, "langfuse_base_url") or "https://us.cloud.langfuse.com"
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return (
        otlp_traces_endpoint(f"{base.rstrip('/')}/api/public/otel"),
        {
            "Authorization": f"Basic {token}",
            "x-langfuse-ingestion-version": "4",
        },
    )


def tracing_active(settings: Any) -> bool:
    if langfuse_configured(settings):
        return True
    if bool(getattr(settings, "otel_traces_enabled", False)):
        return True
    if _setting(settings, "otel_exporter_otlp_endpoint"):
        return True
    return bool(getattr(settings, "otel_traces_console", False))


def _has_remote_exporter(settings: Any) -> bool:
    return bool(_setting(settings, "otel_exporter_otlp_endpoint") or langfuse_configured(settings))


def console_export_enabled(settings: Any) -> bool:
    if bool(getattr(settings, "otel_traces_console", False)):
        return True
    enabled = bool(getattr(settings, "otel_traces_enabled", False))
    is_dev = bool(getattr(settings, "is_dev", False))
    return enabled and is_dev and not _has_remote_exporter(settings)


def otlp_traces_endpoint(endpoint: str) -> str:
    cleaned = (endpoint or "").strip().rstrip("/")
    if cleaned.endswith("/v1/traces"):
        return cleaned
    return f"{cleaned}/v1/traces"


def parse_otlp_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            headers[key] = value
    return headers


def setup_tracing(settings: Settings) -> None:
    """Install a process-wide tracer provider when tracing is configured."""
    global _installed
    if _installed or not tracing_active(settings):
        return
    provider = build_tracer_provider(settings)
    if provider is None:
        return
    trace.set_tracer_provider(provider)
    _installed = True
    logger.info(
        "Conversation tracing on (service=%s langfuse=%s otlp=%s console=%s capture_content=%s)",
        settings.otel_service_name or settings.app_name,
        langfuse_configured(settings),
        bool(_setting(settings, "otel_exporter_otlp_endpoint")),
        console_export_enabled(settings),
        bool(settings.trace_capture_content),
    )


def build_tracer_provider(settings: Any) -> Optional[TracerProvider]:
    service_name = str(getattr(settings, "otel_service_name", "") or "").strip()
    if not service_name:
        service_name = str(getattr(settings, "app_name", "") or "lifeos-api")
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "lifeos",
            "deployment.environment": str(getattr(settings, "app_env", "") or "development"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(_LangfuseAttributeProcessor())
    attached = False

    langfuse = langfuse_otlp(settings)
    if langfuse is not None:
        endpoint, headers = langfuse
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            attached = True
            logger.info("Langfuse traces -> %s", endpoint.split("/api/public/otel")[0])
        except Exception:
            logger.exception("Langfuse trace exporter failed to start")

    endpoint = _setting(settings, "otel_exporter_otlp_endpoint")
    langfuse_endpoint = langfuse[0] if langfuse is not None else ""
    if endpoint and otlp_traces_endpoint(endpoint) != langfuse_endpoint:
        try:
            exporter = OTLPSpanExporter(
                endpoint=otlp_traces_endpoint(endpoint),
                headers=parse_otlp_headers(_setting(settings, "otel_exporter_otlp_headers")),
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            attached = True
        except Exception:
            logger.exception("OTLP trace exporter failed to start")

    if console_export_enabled(settings):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        attached = True

    if not attached:
        return None
    return provider


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        try:
            force_flush(timeout_millis=5000)
        except Exception:
            logger.exception("Failed to flush conversation traces")
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:
            logger.exception("Failed to shut down conversation tracing")


class _LangfuseAttributeProcessor(SpanProcessor):
    """Copy Langfuse trace attributes onto every span in the turn."""

    def on_start(self, span: Span, parent_context: Optional[otel_context.Context] = None) -> None:
        ctx = parent_context if parent_context is not None else otel_context.get_current()
        for key in _LANGFUSE_BAGGAGE:
            value = baggage.get_baggage(key, context=ctx)
            if value:
                span.set_attribute(key, str(value))

    def on_end(self, span: Span) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("lifeos.chat", "0.1.0")


@contextmanager
def _open_span(name: str, kind: SpanKind) -> Iterator[Span]:
    """Start a current span that survives async-generator cleanup.

    Streaming responses can close the generator in another context. Detach is
    best-effort so that mismatch does not fail the chat request.
    """
    span = get_tracer().start_span(name, kind=kind)
    token = otel_context.attach(trace.set_span_in_context(span))
    try:
        yield span
    finally:
        span.end()
        try:
            otel_context.detach(token)
        except ValueError:
            logger.debug("Trace context closed in a different task", exc_info=True)


@contextmanager
def conversation_span(
    name: str,
    *,
    user: Any,
    settings: Any,
    timezone_name: Optional[str] = None,
    content: str = "",
) -> Iterator[Span]:
    user_id = str(getattr(user, "id", "") or "")
    ctx = otel_context.get_current()
    ctx = baggage.set_baggage("langfuse.trace.name", name, context=ctx)
    if user_id:
        ctx = baggage.set_baggage("langfuse.user.id", user_id, context=ctx)
        ctx = baggage.set_baggage("langfuse.session.id", user_id, context=ctx)
    token = otel_context.attach(ctx)
    try:
        with _open_span(name, SpanKind.INTERNAL) as span:
            span.set_attribute("langfuse.trace.name", name)
            annotate_conversation(
                span,
                user=user,
                settings=settings,
                timezone_name=timezone_name,
                content=content,
            )
            try:
                yield span
            except Exception as exc:
                record_error(span, exc)
                raise
    finally:
        try:
            otel_context.detach(token)
        except ValueError:
            logger.debug("Trace context closed in a different task", exc_info=True)


def annotate_conversation(
    span: Span,
    *,
    user: Any,
    settings: Any,
    timezone_name: Optional[str] = None,
    content: str = "",
) -> None:
    mode = getattr(user, "ai_mode", "")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", "gcp.gemini")
    span.set_attribute("gen_ai.request.model", str(getattr(settings, "gemini_model", "") or ""))
    user_id = str(getattr(user, "id", "") or "")
    span.set_attribute("lifeos.user_id", user_id)
    span.set_attribute("langfuse.user.id", user_id)
    if user_id:
        span.set_attribute("langfuse.session.id", user_id)
    span.set_attribute("lifeos.ai_mode", str(getattr(mode, "value", mode) or ""))
    span.set_attribute(
        "lifeos.capture_content", bool(getattr(settings, "trace_capture_content", False))
    )
    if timezone_name:
        span.set_attribute("lifeos.timezone", timezone_name)
    text = (content or "").strip()
    span.set_attribute("lifeos.input_chars", len(text))
    if getattr(settings, "trace_capture_content", False) and text:
        _attach_message(span, "gen_ai.input.messages", "user", text)


def note_context(span: Span, *, prompt: str, history_count: int) -> None:
    span.set_attribute("lifeos.history_messages", int(history_count))
    body = prompt or ""
    span.set_attribute("lifeos.first_chat", "[First chat]" in body)
    span.set_attribute("lifeos.gap_checkin", "[Conversation gap]" in body)


def finish_turn(
    span: Span,
    *,
    settings: Any,
    reply: str,
    actions: list[dict[str, Any]],
    proposed: int,
) -> None:
    applied = list(actions or [])
    types = tuple(str(action.get("type") or "") for action in applied[:20])
    span.set_attribute("lifeos.actions.proposed", int(proposed))
    span.set_attribute("lifeos.actions.applied", len(applied))
    span.set_attribute("lifeos.actions.dropped", max(0, int(proposed) - len(applied)))
    if types:
        span.set_attribute("lifeos.action_types", types)
    text = reply or ""
    span.set_attribute("lifeos.reply_chars", len(text))
    if getattr(settings, "trace_capture_content", False) and text.strip():
        _attach_message(span, "gen_ai.output.messages", "assistant", text)
        summaries = tuple(
            str(action.get("summary") or "")
            for action in applied[:20]
            if action.get("summary")
        )
        if summaries:
            span.set_attribute("lifeos.action_summaries", summaries)


class GenerationTrace:
    """Child span for one Gemini generate call."""

    def __init__(self, span: Span) -> None:
        self.span = span

    def observe(self, response: Any) -> None:
        if response is None:
            return
        usage = getattr(response, "usage_metadata", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage_metadata")
        _set_usage(self.span, usage)
        model = getattr(response, "model_version", None) or getattr(response, "model", None)
        if model:
            self.span.set_attribute("gen_ai.response.model", str(model))
        reasons = _finish_reasons(response)
        if reasons:
            self.span.set_attribute("gen_ai.response.finish_reasons", tuple(reasons))


@contextmanager
def model_span(settings: Any) -> Iterator[GenerationTrace]:
    model = str(getattr(settings, "gemini_model", "") or "gemini")
    with _open_span(f"chat {model}", SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "gcp.gemini")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("langfuse.observation.type", "generation")
        span.set_attribute("langfuse.observation.model.name", model)
        generation = GenerationTrace(span)
        try:
            yield generation
        except Exception as exc:
            record_error(span, exc)
            raise


@contextmanager
def actions_span(proposed: int) -> Iterator[Span]:
    with _open_span("lifeos.chat.apply_actions", SpanKind.INTERNAL) as span:
        span.set_attribute("lifeos.actions.proposed", int(proposed))
        try:
            yield span
        except Exception as exc:
            record_error(span, exc)
            raise


def record_error(span: Span, exc: BaseException) -> None:
    try:
        message = _clip(_redact(_error_message(exc)), _ERROR_LIMIT)
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        span.add_event(
            "exception",
            {
                "exception.type": type(exc).__name__,
                "exception.message": message,
                "exception.stacktrace": _clip(_redact(stack), _CONTENT_LIMIT),
            },
        )
        span.set_status(Status(StatusCode.ERROR, message))
        span.set_attribute("error.type", type(exc).__name__)
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            span.set_attribute("http.response.status_code", status)
    except Exception:
        logger.debug("Could not record conversation span error", exc_info=True)


def _error_message(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str) and detail.strip():
        return detail
    return str(exc) or type(exc).__name__


def _attach_message(span: Span, attribute: str, role: str, content: str) -> None:
    clipped = _clip(content.strip(), _CONTENT_LIMIT)
    payload = json.dumps([{"role": role, "parts": [{"type": "text", "content": clipped}]}], ensure_ascii=False)
    span.set_attribute(attribute, payload)
    if role == "user":
        span.set_attribute("langfuse.observation.input", clipped)
        span.set_attribute("gen_ai.prompt", clipped)
    else:
        span.set_attribute("langfuse.observation.output", clipped)
        span.set_attribute("gen_ai.completion", clipped)
    span.add_event(
        "gen_ai.content.prompt" if role == "user" else "gen_ai.content.completion",
        {"gen_ai.system": "gcp.gemini", "role": role, "content": clipped},
    )


def _set_usage(span: Span, usage: Any) -> None:
    mapping = (
        ("prompt_token_count", "gen_ai.usage.input_tokens"),
        ("candidates_token_count", "gen_ai.usage.output_tokens"),
    )
    for source, target in mapping:
        value = _usage_value(usage, source)
        if value is not None:
            span.set_attribute(target, value)


def _usage_value(usage: Any, name: str) -> Optional[int]:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finish_reasons(response: Any) -> list[str]:
    candidates = getattr(response, "candidates", None) or []
    reasons: list[str] = []
    for candidate in candidates:
        reason = getattr(candidate, "finish_reason", None)
        if reason is None:
            continue
        name = getattr(reason, "name", None)
        reasons.append(str(name or reason))
    return reasons


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _redact(text: str) -> str:
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned
