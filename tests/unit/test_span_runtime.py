"""Runtime span instrumentation (S7, ADR 0021 §3/§4, D71/D74) — the
`SpanRecorder` buffering semantics, the `TracingModelClient` span shapes,
and the no-sink byte-identity. A recording fake sink; no DB, no network."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.domain.message import Usage
from jarvis.domain.spans import SpanRecord
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    StreamDelta,
    TextDelta,
    UsageDelta,
)
from jarvis.ports.spans import SpanSink
from jarvis.runtime.spans import SpanRecorder, TracingModelClient, status_for_run


class RecordingSink(SpanSink):
    """Captures every exported batch; optionally raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.batches: list[Sequence[SpanRecord]] = []
        self._error = error

    async def export(self, spans: Sequence[SpanRecord]) -> None:
        if self._error is not None:
            raise self._error
        self.batches.append(list(spans))


_UNSET: SpanSink | None = object()  # sentinel — None is a meaningful (inactive) sink


def _recorder(sink: SpanSink | None = _UNSET) -> SpanRecorder:  # type: ignore[assignment]
    return SpanRecorder(
        RecordingSink() if sink is _UNSET else sink,
        run_id="run-1",
        trace_id="a" * 32,
        tenant_id="t1",
    )


def _request() -> ModelRequest:
    return ModelRequest(
        model="gemma4:31b",
        messages=[{"role": "user", "content": "hello"}],
    )


class FakeClient:
    """Duck-typed ModelClient (the test_llm_trace pattern): scripted
    deltas/exceptions, records calls."""

    ref = ModelRef(provider="ollama", model="gemma4:31b")
    capabilities = ModelCapabilities()

    def __init__(self, deltas: list[StreamDelta] | None = None, error: Exception | None = None):
        self._deltas = deltas or []
        self._error = error

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        if self._error is not None:
            raise self._error
        return ModelResponse(
            message={  # type: ignore[call-arg]
                "role": "assistant",
                "content": "the answer",
                "tool_calls": [{"id": "c1", "name": "http_get", "arguments": {}}],
            },
            usage={"input_tokens": 11, "output_tokens": 3, "extra": {}},  # type: ignore[call-arg]
        )

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        async def gen() -> AsyncIterator[StreamDelta]:
            for delta in self._deltas:
                yield delta
            if self._error is not None:
                raise self._error

        return gen()


# --- the recorder -------------------------------------------------------------


async def test_start_end_buffers_finished_spans():
    sink = RecordingSink()
    spans = _recorder(sink)
    assert spans.active

    record = spans.start("llm.call", "llm.call", attributes={"provider": "ollama"})
    assert record is not None and record.trace_id == "a" * 32 and record.run_id == "run-1"
    spans.end(record, attributes={"input_tokens": 11})

    assert sink.batches == []  # nothing exported before flush — write ON END
    await spans.flush()
    assert len(sink.batches) == 1
    (span,) = sink.batches[0]
    assert span.name == "llm.call" and span.status == "ok"
    assert span.attributes["provider"] == "ollama"
    assert span.attributes["input_tokens"] == 11
    assert span.duration_ms >= 0.0


async def test_end_ignores_unknown_record_no_double_flush():
    sink = RecordingSink()
    spans = _recorder(sink)
    first = spans.start("iteration", "iteration")
    spans.end(first)
    # Ending the SAME record twice is a no-op (identity check), not an error.
    spans.end(first)
    await spans.flush()
    (span,) = sink.batches[0]
    assert span.name == "iteration"
    assert len(sink.batches[0]) == 1


async def test_slot_close_flush_order_children_before_parents():
    sink = RecordingSink()
    spans = _recorder(sink)
    spans.start("run.segment", "run.segment", slot="segment")
    spans.start("iteration", "iteration", slot="iteration")
    spans.end_slot("iteration", status="ok", attributes={"paused": True})
    spans.end_slot("segment", status="ok")
    await spans.flush()
    (batch,) = sink.batches
    assert [s.name for s in batch] == ["iteration", "run.segment"]
    assert batch[0].attributes["paused"] is True
    # end_slot on an unknown/already-closed slot is a silent no-op
    spans.end_slot("iteration")
    spans.end_slot("never-started")


async def test_flush_force_closes_open_spans_lifo():
    sink = RecordingSink()
    spans = _recorder(sink)
    spans.start("run.segment", "run.segment", slot="segment")
    spans.start("iteration", "iteration", slot="iteration")
    spans.start("tool.call", "tool.call", attributes={"tool": "http_get"})
    await spans.flush(pending_status="error", pending_message="boom")
    (batch,) = sink.batches
    # All three closed by the safety net, LIFO: the deepest first.
    assert [s.name for s in batch] == ["tool.call", "iteration", "run.segment"]
    for span in batch:
        assert span.status == "error" and span.status_message == "boom"
    # A second flush exports nothing — the buffer was drained.
    await spans.flush()
    assert len(sink.batches) == 1


async def test_flush_swallows_sink_failures():
    spans = _recorder(RecordingSink(error=RuntimeError("db down")))
    spans.start("llm.call", "llm.call")
    record = spans.start("tool.call", "tool.call")
    spans.end(record)
    await spans.flush()  # must not raise (D5 transposed)


async def test_inactive_recorder_no_ops_everything():
    spans = _recorder(None)
    assert not spans.active
    assert spans.start("iteration", "iteration", slot="iteration") is None
    spans.end(None)
    spans.end_slot("iteration")
    await spans.flush()  # no-op, no crash


async def test_odd_trace_id_falls_back_to_local_mint():
    spans = SpanRecorder(RecordingSink(), run_id="run-1", trace_id="nope")
    record = spans.start("iteration", "iteration")
    assert record is not None
    assert len(record.trace_id) == 32 and all(c in "0123456789abcdef" for c in record.trace_id)


def test_status_for_run_mapping():
    assert status_for_run("succeeded") == "ok"
    assert status_for_run("cancelled") == "cancelled"
    assert status_for_run("failed") == "error"
    assert status_for_run("timed_out") == "error"


# --- the tracing model client -------------------------------------------------


async def test_generate_wraps_one_llm_call_span():
    sink = RecordingSink()
    spans = _recorder(sink)
    client = TracingModelClient(FakeClient(), spans)

    response = await client.generate(_request())
    assert response.message.content == "the answer"  # delegation intact

    await spans.flush()
    (span,) = sink.batches[0]
    assert span.kind == "llm.call" and span.status == "ok"
    assert span.attributes["provider"] == "ollama"
    assert span.attributes["model"] == "gemma4:31b"
    assert span.attributes["method"] == "generate"
    assert span.attributes["input_tokens"] == 11
    assert span.attributes["output_tokens"] == 3
    assert span.attributes["finish_reason"] == "stop"
    assert span.attributes["tool_calls"] == "http_get"


async def test_generate_error_sets_span_error_status():
    sink = RecordingSink()
    spans = _recorder(sink)
    client = TracingModelClient(FakeClient(error=RuntimeError("connection refused")), spans)

    with pytest.raises(RuntimeError):
        await client.generate(_request())

    await spans.flush()
    (span,) = sink.batches[0]
    assert span.status == "error"
    assert "RuntimeError" in span.status_message and "connection refused" in span.status_message


async def test_stream_passes_deltas_and_tracks_usage():
    sink = RecordingSink()
    spans = _recorder(sink)
    client = TracingModelClient(
        FakeClient(
            deltas=[
                TextDelta(text="hi"),
                UsageDelta(usage=Usage(input_tokens=5, output_tokens=2)),
                FinishDelta(finish_reason="tool_calls"),
            ]
        ),
        spans,
    )

    seen = [delta async for delta in client.stream(_request())]
    assert [d.type for d in seen] == ["text", "usage", "finish"]  # byte-identical pass-through

    await spans.flush()
    (span,) = sink.batches[0]
    assert span.kind == "llm.call" and span.status == "ok"
    assert span.attributes["method"] == "stream"
    assert span.attributes["input_tokens"] == 5
    assert span.attributes["output_tokens"] == 2
    assert span.attributes["finish_reason"] == "tool_calls"


async def test_stream_error_sets_span_error():
    sink = RecordingSink()
    spans = _recorder(sink)
    client = TracingModelClient(FakeClient(error=ValueError("bad payload")), spans)

    with pytest.raises(ValueError):
        async for _ in client.stream(_request()):
            pass

    await spans.flush()
    (span,) = sink.batches[0]
    assert span.status == "error" and "ValueError" in span.status_message


async def test_stream_generator_exit_marks_cancelled():
    sink = RecordingSink()
    spans = _recorder(sink)
    client = TracingModelClient(
        FakeClient(deltas=[TextDelta(text="a"), TextDelta(text="b")]), spans
    )

    gen = client.stream(_request())
    assert (await gen.__anext__()).text == "a"
    await gen.aclose()  # consumer stops early — abort, not model failure

    await spans.flush()
    (span,) = sink.batches[0]
    assert span.status == "cancelled"
    assert span.status_message == "stream closed early"


async def test_traced_client_inherits_ref_and_capabilities():
    inner = FakeClient()
    client = TracingModelClient(inner, _recorder())
    assert client.ref is inner.ref
    assert client.capabilities is inner.capabilities
