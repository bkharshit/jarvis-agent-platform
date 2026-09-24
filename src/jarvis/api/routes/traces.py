"""Traces: the spans table's read side (S7, ADR 0021 §7, D77). Tenant
scoped — a trace's runs and children all carry the same trace_id (D73),
so one listing is the whole multi-agent conversation."""

from __future__ import annotations

from fastapi import APIRouter

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.routes.capabilities import ContainerDep
from jarvis.api.schemas import TraceDetail, TraceList, TraceSummary
from jarvis.persistence.repositories import SqlSpanRepo

router = APIRouter(prefix="/traces", tags=["traces"])


def _repo(container: AppContainer) -> SqlSpanRepo:
    """The spans read side — None exactly when persistence is off (D72);
    the section then reports unavailable, never fakes empty."""
    spans = container.spans
    if spans is None:
        raise ApiError(503, "observability_disabled", "span persistence is disabled")
    return spans


@router.get("")
async def list_traces(
    limit: int = 50,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> TraceList:
    spans = _repo(container)
    rows = await spans.list_traces(tenant_id=auth.principal.tenant_id, limit=limit)
    return TraceList(
        traces=[
            TraceSummary(
                trace_id=row["trace_id"],
                run_ids=row["run_ids"],
                span_count=row["span_count"],
                statuses=sorted(row["statuses"]),
                kinds=sorted(row["kinds"]),
                started_at=row["started_at"],
                ended_at=row["ended_at"],
            )
            for row in rows
        ]
    )


@router.get("/{trace_id}")
async def trace_detail(
    trace_id: str,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> TraceDetail:
    spans = _repo(container)
    spans_list = await spans.get_trace(trace_id, tenant_id=auth.principal.tenant_id)
    if not spans_list:
        raise ApiError(404, "not_found", f"trace {trace_id!r} not found")
    return TraceDetail(trace_id=trace_id, spans=spans_list)


__all__ = ["router"]
