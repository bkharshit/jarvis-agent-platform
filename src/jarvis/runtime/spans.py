"""Span instrumentation (S7, ADR 0021 §3/§4, D71/D74).

`SpanRecorder` is the per-run buffer between the runtime's instrumentation
points and the `SpanSink` port: spans are built around boundaries that
already exist (segment, iteration, model call, tool call, retrieval) and
flushed at iteration boundaries, pauses, and terminals — in a `finally` on
every failure path, sink exceptions logged and swallowed (D5 transposed:
observability never touches a run's outcome). With a None sink the recorder
is a full no-op and the runtime is byte-identical (the golden gates).

Two conventions:

- **Slots** — the segment and iteration spans start and end at widely
  separated sites (the iteration spans one loop-body pass that pauses,
  fails, or completes in several places), so `start(..., slot=)` /
  `end_slot()` close the same record without threading a handle through
  signatures. A recorder is per-run, so slot state never collides.
- **Force-close safety net** — `flush()` closes any still-open span before
  exporting, so a cancellation or an exception mid-iteration still yields a
  complete (closed) record set. Spans are written ON END only.

`TracingModelClient` wraps `ModelClient` by pure delegation (the
`TracedModelClient` pattern — client-level wrapping catches EVERY strategy
including plugins; streaming deltas pass through byte-identical) and emits
one `llm.call` span per call.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.domain.message import Usage
from jarvis.domain.spans import SpanKind, SpanRecord, SpanStatus
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    StreamDelta,
    UsageDelta,
)
from jarvis.ports.model import ModelClient
from jarvis.ports.spans import SpanSink

logger = logging.getLogger("jarvis.spans")


class SpanRecorder:
    """One run's span buffer. `active` is False exactly when there is no
    sink — instrumentation still calls in, every method no-ops."""

    def __init__(
        self,
        sink: SpanSink | None,
        *,
        run_id: str,
        trace_id: str,
        tenant_id: str | None = None,
    ) -> None:
        self._sink = sink
        self.active = sink is not None
        self._run_id = run_id
        # The trace_id IS the run's persisted trace_id (D73 — every mint
        # site produces 32 hex); an absent or pre-S7 one (legacy dash-UUID
        # rows, a direct runtime call outside the queue path) mints locally
        # so the record stays a valid OTel trace id.
        self._trace_id = trace_id if len(trace_id) == 32 else uuid.uuid4().hex
        self._tenant_id = tenant_id
        self._buffer: list[SpanRecord] = []
        self._pending: list[SpanRecord] = []  # open spans, in start order
        self._slots: dict[str, SpanRecord] = {}

    def start(
        self,
        name: str,
        kind: SpanKind,
        *,
        slot: str | None = None,
        parent: SpanRecord | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> SpanRecord | None:
        if not self.active:
            return None
        record = SpanRecord(
            trace_id=self._trace_id,
            run_id=self._run_id,
            tenant_id=self._tenant_id,
            name=name,
            kind=kind,
            parent_span_id=parent.span_id if parent is not None else None,
            attributes={k: v for k, v in (attributes or {}).items() if v is not None},
        )
        self._pending.append(record)
        if slot is not None:
            self._slots[slot] = record
        return record

    def end(
        self,
        record: SpanRecord | None,
        *,
        status: SpanStatus = "ok",
        status_message: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Close one open span (identity-checked: an already-closed record
        is ignored, never re-flushed)."""
        if record is None or not self.active:
            return
        for index, pending in enumerate(self._pending):
            if pending is record:
                del self._pending[index]
                break
        else:
            return
        record.ended_at = datetime.now(UTC)
        record.duration_ms = (record.ended_at - record.started_at).total_seconds() * 1000.0
        record.status = status
        record.status_message = status_message
        if attributes:
            record.attributes.update({k: v for k, v in attributes.items() if v is not None})
        self._buffer.append(record)

    def end_slot(self, slot: str, **kwargs: Any) -> None:
        self.end(self._slots.pop(slot, None), **kwargs)

    async def flush(
        self,
        *,
        pending_status: SpanStatus = "cancelled",
        pending_message: str | None = None,
    ) -> None:
        """Force-close still-open spans (LIFO — children before parents) and
        export the buffer. Sink failures are logged and swallowed."""
        while self._pending:
            self.end(self._pending[-1], status=pending_status, status_message=pending_message)
        if self._sink is None or not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            await self._sink.export(batch)
        except Exception:  # noqa: BLE001 — observability never breaks a run (D5)
            logger.warning(
                "span export failed for run %s; %d spans dropped",
                self._run_id,
                len(batch),
                exc_info=True,
            )


def status_for_run(status: str) -> SpanStatus:
    """Map a RunResult status onto a span status (ADR 0021 §3)."""
    if status == "succeeded":
        return "ok"
    if status in ("cancelled",):
        return "cancelled"
    return "error"  # failed, timed_out, anything unexpected


class TracingModelClient:
    """ModelClient by pure delegation with one `llm.call` span per call.
    Deltas pass through unmodified — streaming behavior is byte-identical;
    only the span differs. The strategy cannot tell the difference."""

    def __init__(self, client: ModelClient, recorder: SpanRecorder) -> None:
        self._client = client
        self._recorder = recorder

    @property
    def ref(self) -> ModelRef:
        return self._client.ref

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._client.capabilities

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        span = self._recorder.start(
            "llm.call",
            "llm.call",
            attributes=self._request_attrs("generate", request),
        )
        try:
            response = await self._client.generate(request, cancel=cancel)
        except Exception as exc:
            self._recorder.end(span, status="error", status_message=f"{type(exc).__name__}: {exc}")
            raise
        self._recorder.end(span, attributes=self._response_attrs(response))
        return response

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        span = self._recorder.start(
            "llm.call",
            "llm.call",
            attributes=self._request_attrs("stream", request),
        )
        return self._traced_stream(span, request, cancel=cancel)

    async def _traced_stream(
        self, span: SpanRecord | None, request: ModelRequest, *, cancel: CancellationToken | None
    ) -> AsyncIterator[StreamDelta]:
        usage = Usage()
        finish_reason = "stop"
        try:
            async for delta in self._client.stream(request, cancel=cancel):
                if isinstance(delta, UsageDelta):
                    usage = delta.usage
                elif isinstance(delta, FinishDelta):
                    finish_reason = delta.finish_reason
                yield delta
        except GeneratorExit:
            # The consumer stopped early — an abort, not a model failure.
            self._recorder.end(span, status="cancelled", status_message="stream closed early")
            raise
        except Exception as exc:
            self._recorder.end(span, status="error", status_message=f"{type(exc).__name__}: {exc}")
            raise
        self._recorder.end(span, attributes=self._response_attrs_usage(finish_reason, usage))

    def _request_attrs(self, method: str, request: ModelRequest) -> dict[str, Any]:
        ref = self._client.ref
        return {
            "provider": ref.provider,
            "model": ref.model,
            "method": method,
        }

    def _response_attrs(self, response: ModelResponse) -> dict[str, Any]:
        names = [call.name for call in (response.message.tool_calls or [])]
        return {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "finish_reason": response.finish_reason,
            "tool_calls": ",".join(names) if names else None,
        }

    def _response_attrs_usage(self, finish_reason: str, usage: Usage) -> dict[str, Any]:
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "finish_reason": finish_reason,
        }


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["SpanRecorder", "TracingModelClient", "status_for_run"]
