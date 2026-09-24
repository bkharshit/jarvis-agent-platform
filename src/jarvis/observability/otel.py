"""OTLP export adapter (S7, ADR 0021 §5, D72/D75) — the runtime's finished
`SpanRecord`s over the real OpenTelemetry SDK's OTLP/HTTP exporter.

The runtime imports NO opentelemetry anything; this adapter is the only
place the SDK exists. Spans arrive already FINISHED (D74 — write-on-end),
so no TracerProvider/BatchSpanProcessor is needed: the batch is converted
to SDK `ReadableSpan`s (explicit `SpanContext`, the run's trace_id as the
OTel trace id per D73) and handed to the exporter directly, preserving the
recorder's flush cadence. Export failures are logged and swallowed —
observability never touches a run's outcome (D5 transposed).

The SDK dependency is imported lazily at construction so the rest of the
platform (and every test that never builds this adapter) runs without it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from jarvis.config import Settings
from jarvis.domain.spans import SpanRecord

logger = logging.getLogger("jarvis.spans")

# The OTel status set has no "cancelled" — a cancelled span maps to UNSET
# (no error semantics); error keeps its message as the description.
_STATUS_MAP = {"ok": "OK", "error": "ERROR", "cancelled": "UNSET"}


def parse_headers_env(value: str) -> dict[str, str]:
    """Parse the SDK headers format ("api-key=secret,env=prod") from an env
    var's VALUE. Malformed entries are skipped (logged) — a bad header must
    not take the exporter down."""
    headers: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, val = item.partition("=")
        if not sep or not key.strip():
            logger.warning("malformed OTLP header entry %r skipped", item)
            continue
        headers[key.strip()] = val.strip()
    return headers


class _Exporter(Protocol):
    """The SDK `SpanExporter` shape, kept loose so the SDK stays off this
    module's top-level imports."""

    def export(self, spans: Sequence[Any]) -> Any: ...

    def shutdown(self) -> None: ...


class OtelSpanSink:
    """SpanSink → OTLP/HTTP. Builds the real exporter from Settings
    (`from_settings`); tests inject a fake through the plain constructor."""

    def __init__(self, exporter: _Exporter, service_name: str) -> None:
        self._exporter = exporter
        self._resource: Any = None  # the SDK Resource, built lazily
        self._service_name = service_name

    @classmethod
    def from_settings(cls, settings: Settings) -> OtelSpanSink | None:
        """Build the real exporter behind JARVIS_OTEL_*; None when disabled
        or the SDK is unavailable (logged once — the platform keeps running
        without export)."""
        if not settings.otel_export_enabled:
            return None
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            logger.warning("opentelemetry-sdk not installed; OTLP export disabled")
            return None
        headers: dict[str, str] = {}
        if settings.otel_headers_env:
            raw = os.environ.get(settings.otel_headers_env, "")
            if raw:
                headers = parse_headers_env(raw)
        return cls(
            OTLPSpanExporter(endpoint=settings.otel_endpoint, headers=headers or None),
            service_name=settings.otel_service_name,
        )

    async def export(self, spans: Sequence[SpanRecord]) -> None:
        """Convert one finished batch to SDK ReadableSpans and export."""
        if not spans:
            return
        from opentelemetry.sdk.trace.export import SpanExportResult

        batch = [self._convert(record) for record in spans]
        result = self._exporter.export(batch)
        if result is not SpanExportResult.SUCCESS:
            logger.warning("OTLP export failed for %d spans: %s", len(batch), result)

    def aclose(self) -> None:
        """Flush and release the exporter (process shutdown)."""
        shutdown = getattr(self._exporter, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001 — observability never breaks shutdown
                logger.warning("OTLP exporter shutdown failed", exc_info=True)

    def _convert(self, record: SpanRecord) -> object:
        """SpanRecord → SDK ReadableSpan with an explicit SpanContext."""
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
        from opentelemetry.trace import SpanContext, SpanKind, Status, TraceFlags
        from opentelemetry.trace.status import StatusCode

        if self._resource is None:
            self._resource = Resource.create({"service.name": self._service_name})

        trace_id = int(record.trace_id, 16)
        context = SpanContext(
            trace_id=trace_id,
            span_id=int(record.span_id, 16),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        parent = (
            SpanContext(
                trace_id=trace_id,
                span_id=int(record.parent_span_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            if record.parent_span_id is not None
            else None
        )
        status_code = StatusCode[_STATUS_MAP[record.status]]
        status = Status(
            status_code,
            description=record.status_message if status_code is StatusCode.ERROR else None,
        )
        # OTel attributes cannot be None-valued.
        attributes = {k: v for k, v in record.attributes.items() if v is not None}
        return ReadableSpan(
            name=record.name,
            context=context,
            parent=parent,
            resource=self._resource,
            attributes=attributes or None,
            kind=SpanKind.INTERNAL,
            instrumentation_scope=InstrumentationScope(name="jarvis"),
            start_time=_ns(record.started_at),
            end_time=_ns(record.ended_at),
            status=status,
        )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ns(moment: datetime) -> int:
    """datetime → epoch nanoseconds (the SDK's time unit) — exact
    integer arithmetic (a float timestamp loses ns digits)."""
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


__all__ = ["OtelSpanSink", "parse_headers_env"]
