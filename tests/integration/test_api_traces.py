"""Traces routes over HTTP (S7, ADR 0021 §7, D77): the spans table's read
side — list grouped newest-first, one trace's chronological waterfall,
strict tenancy, the 404, and the capabilities flip. Spans are seeded
through the repo (the run-path e2e lives in test_traces_e2e.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.domain.spans import SpanRecord

pytestmark = pytest.mark.db

TENANT = "default"  # anonymous auth → the seed tenant
OTHER_TENANT = "tenant-b"

TRACE_A = "a" * 32
TRACE_B = "b" * 32


async def _seed(container, trace_id: str, run_id: str, tenant_id: str, *, status: str = "ok"):
    """Two spans (parent segment + child llm.call) in one trace."""
    start = datetime.now(UTC) - timedelta(seconds=5)
    segment = SpanRecord(
        trace_id=trace_id,
        run_id=run_id,
        tenant_id=tenant_id,
        span_id="1" * 16,
        name="run.segment",
        kind="run.segment",
        started_at=start,
        ended_at=start + timedelta(milliseconds=100),
        duration_ms=100.0,
    )
    call = SpanRecord(
        trace_id=trace_id,
        run_id=run_id,
        tenant_id=tenant_id,
        span_id="2" * 16,
        parent_span_id=segment.span_id,
        name="llm.call",
        kind="llm.call",
        started_at=start + timedelta(milliseconds=10),
        ended_at=start + timedelta(milliseconds=90),
        duration_ms=80.0,
        status=status,
        status_message="boom" if status == "error" else None,
    )
    assert container.spans is not None
    await container.spans.export([segment, call])


async def test_empty_state_lists_nothing(client):
    resp = await client.get("/v1/traces")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"traces": []}


async def test_list_groups_by_trace_newest_first(client, container):
    await _seed(container, TRACE_A, "run-1", TENANT)
    await _seed(container, TRACE_B, "run-2", TENANT)
    resp = await client.get("/v1/traces")
    assert resp.status_code == 200, resp.text
    traces = resp.json()["traces"]
    assert [t["trace_id"] for t in traces] == [TRACE_B, TRACE_A]  # newest first
    first = traces[0]
    assert first["run_ids"] == ["run-2"]
    assert first["span_count"] == 2
    assert first["statuses"] == ["ok"]
    assert first["kinds"] == ["llm.call", "run.segment"]


async def test_detail_returns_chronological_spans(client, container):
    await _seed(container, TRACE_A, "run-1", TENANT)
    resp = await client.get(f"/v1/traces/{TRACE_A}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trace_id"] == TRACE_A
    assert [s["span_id"] for s in body["spans"]] == ["1" * 16, "2" * 16]
    assert body["spans"][1]["parent_span_id"] == "1" * 16


async def test_detail_404_for_unknown_trace(client):
    resp = await client.get(f"/v1/traces/{'c' * 32}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["kind"] == "not_found"


async def test_tenants_never_see_each_others_traces(client, container):
    await container.auth.create_tenant(OTHER_TENANT, OTHER_TENANT)
    await _seed(container, TRACE_A, "run-1", TENANT)
    assert container.spans is not None
    # Another tenant's row exists, under a different tenant id.
    await _seed(container, TRACE_B, "run-2", OTHER_TENANT)
    listed = (await client.get("/v1/traces")).json()["traces"]
    assert [t["trace_id"] for t in listed] == [TRACE_A]  # the caller's tenant only
    resp = await client.get(f"/v1/traces/{TRACE_B}")
    assert resp.status_code == 404


async def test_capabilities_reports_observability_live(client):
    resp = await client.get("/v1/capabilities")
    assert resp.status_code == 200, resp.text
    section = resp.json()["sections"]["observability"]
    assert section["enabled"] is True
    assert section["stage"] is None  # no longer a coming-soon section
    assert section["detail"] == {
        "spans": True,
        "otel_export": False,
        "service_name": "jarvis",
    }
