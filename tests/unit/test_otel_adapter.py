"""OTLP export adapter + observability settings (S7, ADR 0021 §5,
D72/D75) — record→ReadableSpan conversion against a fake exporter, the
headers-env parsing, and the disabled paths. The real SDK is installed
(the conversion needs it); no network, no collector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanKind, StatusCode

from jarvis.config import Settings
from jarvis.domain.spans import SpanRecord
from jarvis.observability.otel import OtelSpanSink, parse_headers_env


class FakeExporter:
    """Captures the converted batch; optionally reports failure."""

    def __init__(self, success: bool = True) -> None:
        self.batches: list[list[Any]] = []
        self._success = success
        self.shutdowns = 0

    def export(self, spans: list[Any]) -> SpanExportResult:
        self.batches.append(spans)
        return SpanExportResult.SUCCESS if self._success else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self.shutdowns += 1


def _sink() -> tuple[OtelSpanSink, FakeExporter]:
    exporter = FakeExporter()
    return OtelSpanSink(exporter, service_name="jarvis-test"), exporter


def _record(**overrides: Any) -> SpanRecord:
    start = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    defaults: dict[str, Any] = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "run_id": "run-1",
        "tenant_id": "t1",
        "name": "llm.call",
        "kind": "llm.call",
        "started_at": start,
        "ended_at": start + timedelta(milliseconds=250),
        "duration_ms": 250.0,
        "status": "ok",
        "status_message": None,
        "attributes": {"provider": "ollama", "input_tokens": 11},
    }
    defaults.update(overrides)
    return SpanRecord(**defaults)


async def test_convert_builds_real_readable_span():
    sink, exporter = _sink()
    await sink.export([_record()])

    (span,) = exporter.batches[0]
    context = span.get_span_context()
    assert context.trace_id == int("a" * 32, 16)
    assert context.span_id == int("b" * 16, 16)
    assert context.is_remote is False
    assert span.name == "llm.call"
    assert span.kind is SpanKind.INTERNAL
    assert dict(span.attributes)["provider"] == "ollama"
    # Timing in epoch nanoseconds.
    assert span.start_time == int(
        datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC).timestamp() * 1_000_000_000
    )
    assert span.end_time - span.start_time == 250_000_000


async def test_parent_link_and_error_status_convert():
    sink, exporter = _sink()
    await sink.export(
        [
            _record(
                parent_span_id="c" * 16,
                status="error",
                status_message="RuntimeError: boom",
            )
        ]
    )
    (span,) = exporter.batches[0]
    assert span.parent.span_id == int("c" * 16, 16)
    assert span.parent.trace_id == int("a" * 32, 16)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "RuntimeError: boom"


async def test_ok_and_cancelled_have_no_error_semantics():
    sink, exporter = _sink()
    await sink.export([_record(), _record(status="cancelled", status_message="stream closed")])
    ok, cancelled = exporter.batches[0]
    assert ok.status.status_code is StatusCode.OK
    # OTel has no cancelled status — UNSET, and the message is not a description.
    assert cancelled.status.status_code is StatusCode.UNSET
    assert cancelled.status.description is None


async def test_service_name_rides_the_resource():
    sink, exporter = _sink()
    await sink.export([_record()])
    resource = exporter.batches[0][0].resource
    assert isinstance(resource, Resource)
    assert resource.attributes["service.name"] == "jarvis-test"


async def test_export_failure_is_swallowed(caplog: pytest.LogCaptureFixture):
    caplog.set_level("WARNING", logger="jarvis.spans")
    exporter = FakeExporter(success=False)
    sink = OtelSpanSink(exporter, service_name="jarvis-test")
    await sink.export([_record()])  # must not raise (D5 transposed)
    assert any("OTLP export failed" in r.message for r in caplog.records)


async def test_empty_batch_never_touches_the_exporter():
    sink, exporter = _sink()
    await sink.export([])
    assert exporter.batches == []


async def test_aclose_shuts_down_the_exporter():
    sink, exporter = _sink()
    sink.aclose()
    assert exporter.shutdowns == 1


# --- settings-derived construction --------------------------------------------


def test_from_settings_disabled_returns_none():
    assert OtelSpanSink.from_settings(Settings(otel_export_enabled=False, _env_file=None)) is None


def test_from_settings_enabled_builds_the_real_exporter():
    sink = OtelSpanSink.from_settings(
        Settings(
            otel_export_enabled=True,
            otel_endpoint="http://collector:4318/v1/traces",
            _env_file=None,
        )
    )
    assert sink is not None
    # The real exporter is in place; nothing fires without an export call.
    assert hasattr(sink, "export")


def test_settings_defaults_match_the_adr():
    settings = Settings(_env_file=None)
    assert settings.spans_enabled is True
    assert settings.otel_export_enabled is False
    assert settings.otel_endpoint == "http://localhost:4318/v1/traces"
    assert settings.otel_service_name == "jarvis"
    assert settings.otel_headers_env == ""


# --- headers-env parsing --------------------------------------------------------


def test_parse_headers_env_sdk_format():
    assert parse_headers_env("api-key=secret,env=prod") == {"api-key": "secret", "env": "prod"}
    assert parse_headers_env(" k = v , x=y ") == {"k": "v", "x": "y"}


def test_parse_headers_env_skips_malformed_entries():
    assert parse_headers_env("good=1,broken,=novalue,,") == {"good": "1"}
    assert parse_headers_env("") == {}
