"""SqlSpanRepo integration (S7, ADR 0021 §7, D77): migration 0015 applies,
`export` persists a batch (the SpanSink write), reads are tenant-scoped,
and traces group by trace_id."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jarvis.domain.spans import SpanRecord
from jarvis.persistence.repositories import SqlSpanRepo

pytestmark = pytest.mark.db

TENANT = "default"
OTHER_TENANT = "tenant-b"


def _repo(container) -> SqlSpanRepo:
    return SqlSpanRepo(async_sessionmaker(container.engine, expire_on_commit=False))


def _span(
    *,
    trace_id: str,
    run_id: str,
    name: str = "run.segment",
    kind: str = "run.segment",
    tenant_id: str | None = TENANT,
    status: str = "ok",
    offset_ms: float = 0.0,
) -> SpanRecord:
    started = datetime.now(UTC) + timedelta(milliseconds=offset_ms)
    return SpanRecord(
        trace_id=trace_id,
        run_id=run_id,
        tenant_id=tenant_id,
        name=name,
        kind=kind,  # type: ignore[arg-type] — the repo stores the string
        started_at=started,
        ended_at=started + timedelta(milliseconds=10),
        duration_ms=10.0,
        status=status,  # type: ignore[arg-type]
        attributes={"jarvis.event.cursor": 3},
    )


async def test_export_and_read_back_roundtrip(container) -> None:
    repo = _repo(container)
    trace, run = uuid4().hex, str(uuid4())
    parent = _span(trace_id=trace, run_id=run)
    child = _span(trace_id=trace, run_id=run, name="llm.call", kind="llm.call", offset_ms=5)
    child.parent_span_id = parent.span_id
    await repo.export([parent, child])

    spans = await repo.list_spans(run, tenant_id=TENANT)
    assert [s.name for s in spans] == ["run.segment", "llm.call"]
    assert spans[0].trace_id == trace
    assert spans[1].parent_span_id == parent.span_id
    assert spans[0].attributes == {"jarvis.event.cursor": 3}

    trace_spans = await repo.get_trace(trace, tenant_id=TENANT)
    assert [s.span_id for s in trace_spans] == [parent.span_id, child.span_id]


async def test_reads_are_tenant_scoped(container) -> None:
    repo = _repo(container)
    trace, run = uuid4().hex, str(uuid4())
    await repo.export([_span(trace_id=trace, run_id=run)])

    assert await repo.list_spans(run, tenant_id=OTHER_TENANT) == []
    assert await repo.get_trace(trace, tenant_id=OTHER_TENANT) == []
    traces = await repo.list_traces(tenant_id=OTHER_TENANT)
    assert all(t["trace_id"] != trace for t in traces)


async def test_list_traces_groups_by_trace_id(container) -> None:
    repo = _repo(container)
    trace_a, trace_b = uuid4().hex, uuid4().hex
    run_a1, run_a2, run_b = str(uuid4()), str(uuid4()), str(uuid4())
    await repo.export(
        [
            _span(trace_id=trace_a, run_id=run_a1, offset_ms=0),
            _span(trace_id=trace_a, run_id=run_a1, name="llm.call", kind="llm.call", offset_ms=2),
            # a child run shares the parent trace (D73)
            _span(trace_id=trace_a, run_id=run_a2, offset_ms=4),
            _span(trace_id=trace_b, run_id=run_b, status="error", offset_ms=6),
        ]
    )
    traces = {t["trace_id"]: t for t in await repo.list_traces(tenant_id=TENANT)}
    assert traces[trace_a]["span_count"] == 3
    assert sorted(traces[trace_a]["run_ids"]) == sorted([run_a1, run_a2])
    assert traces[trace_a]["kinds"] == {"run.segment", "llm.call"}
    assert traces[trace_b]["statuses"] == {"error"}


async def test_empty_export_is_a_noop(container) -> None:
    await _repo(container).export([])  # must not raise / must not write
