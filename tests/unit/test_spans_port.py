"""SpanSink port (S7, ADR 0021 §1/§4): the fanout tees a batch to every
sink and isolates failures — one broken sink never blocks the others."""

from __future__ import annotations

from uuid import uuid4

import pytest

from jarvis.domain.spans import SpanRecord
from jarvis.ports.spans import FanoutSpanSink, SpanSink


def _record() -> SpanRecord:
    return SpanRecord(
        trace_id=uuid4().hex, run_id=str(uuid4()), name="run.segment", kind="run.segment"
    )


class RecordingSink:
    def __init__(self) -> None:
        self.batches: list[list[SpanRecord]] = []

    async def export(self, spans) -> None:
        self.batches.append(list(spans))


class BrokenSink:
    async def export(self, spans) -> None:
        raise RuntimeError("sink exploded")


async def test_fanout_delivers_to_all_sinks():
    first, second = RecordingSink(), RecordingSink()
    sink = FanoutSpanSink(first, second)
    assert isinstance(sink, SpanSink)  # structural: the port admits it
    batch = [_record(), _record()]
    await sink.export(batch)
    assert first.batches == [batch]
    assert second.batches == [batch]


async def test_fanout_isolates_a_broken_sink():
    good = RecordingSink()
    sink = FanoutSpanSink(BrokenSink(), good)
    await sink.export([_record()])  # the broken sink logs; the run is untouched
    assert len(good.batches) == 1


async def test_fanout_empty_batch_reaches_no_sink():
    good = RecordingSink()
    sink = FanoutSpanSink(good)
    await sink.export([])
    assert good.batches == []


def test_span_record_defaults() -> None:
    record = _record()
    assert len(record.span_id) == 16
    assert record.status == "ok"
    assert record.tenant_id is None  # the sink falls back to the default tenant


@pytest.mark.parametrize(
    "bad",
    [
        {"trace_id": "short"},
        {"kind": "nope"},
        {"status": "nope"},
        {"extra": 1},
    ],
)
def test_span_record_rejects_bad_shapes(bad: dict[str, object]) -> None:
    import pydantic

    fields = {
        "trace_id": uuid4().hex,
        "run_id": str(uuid4()),
        "name": "run.segment",
        "kind": "run.segment",
    }
    fields.update(bad)  # type: ignore[arg-type]
    with pytest.raises(pydantic.ValidationError):
        SpanRecord(**fields)  # type: ignore[arg-type]
