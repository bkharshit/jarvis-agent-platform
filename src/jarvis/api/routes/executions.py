"""Executions: list, detail, cancel (idempotent), event replay (plan §5).

`GET /{id}/events` returns JSON replay by default or an SSE stream when the
client sends `Accept: text/event-stream` — same cursor space as the
Last-Event-ID either way (ADR 0003). The SSE branch tails the database via
PgEventStream (ADR 0008), so it follows runs executed by any worker."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.routes.agents import ContainerDep, _await_terminal_row
from jarvis.api.schemas import (
    CancelResult,
    CursorEvent,
    EventList,
    ExecutionDetail,
    ExecutionList,
)
from jarvis.api.sse import SSE_HEADERS, frame
from jarvis.domain.events import is_terminal
from jarvis.domain.execution import TERMINAL_STATUSES, ExecutionStatus, RunResult
from jarvis.persistence.scoped import TenantScopedExecutions

router = APIRouter(prefix="/executions", tags=["executions"])


async def _require_run(executions: TenantScopedExecutions, run_id: str) -> RunResult:
    """`executions` is the caller's tenant-scoped view — a foreign run is
    simply not found (no existence leak)."""
    run = await executions.get(run_id)
    if run is None:
        raise ApiError(404, "not_found", f"execution {run_id!r} not found")
    return run


@router.get("")
async def list_executions(
    agent_id: str | None = None,
    status: ExecutionStatus | None = None,
    session_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    auth: AuthContext = AuthDep,
) -> ExecutionList:
    items = await auth.executions.list_runs(
        agent_id=agent_id,
        status=status,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return ExecutionList(items=items)


@router.get("/{run_id}")
async def get_execution(run_id: str, auth: AuthContext = AuthDep) -> ExecutionDetail:
    run = await _require_run(auth.executions, run_id)
    messages = await auth.executions.list_messages(run_id)
    tool_executions = await auth.executions.list_tool_executions(run_id)
    return ExecutionDetail(run=run, messages=messages, tool_executions=tool_executions)


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> CancelResult:
    """Idempotent. A run live in THIS process gets its runtime token; a
    queued or foreign-worker run gets a cross-process cancel request
    (ADR 0008 §6) that the owning worker's heartbeat pops. A finished run is
    a no-op that reports its current status."""
    run = await _require_run(auth.executions, run_id)
    if run.status in TERMINAL_STATUSES:
        return CancelResult(run_id=run_id, cancelled=False, status=run.status)
    if run.status == "running" and container.runtime.cancel(run_id):
        return CancelResult(run_id=run_id, cancelled=True, status=run.status)
    await container.queue.request_cancel(run_id, "cancelled by user")
    return CancelResult(run_id=run_id, cancelled=True, status=run.status)


@router.get("/{run_id}/events", response_model=EventList)
async def list_events(
    run_id: str,
    request: Request,
    after: int | None = None,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> EventList | StreamingResponse:
    await _require_run(auth.executions, run_id)  # 404 when unknown
    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _live_replay(container, auth.executions, run_id, after),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
    pairs = [
        CursorEvent(cursor=cursor, event=event)
        async for cursor, event in auth.executions.replay_with_cursor(run_id, after)
    ]
    return EventList(run_id=run_id, after=after, events=pairs)


async def _live_replay(
    container: AppContainer,
    executions: TenantScopedExecutions,
    run_id: str,
    after: int | None,
) -> AsyncIterator[str]:
    """Replay from `after`, then live-tail until the run's terminal event
    (a finished run's stream is a pure replay — the terminal ends it). The
    terminal frame is held back until the run row is terminal, mirroring the
    stream route, so stream end carries the run's final state."""
    last_frame: str | None = None
    async for cursor, event in container.streams.subscribe(run_id, after):
        if is_terminal(event):
            last_frame = frame(cursor, event)  # subscribe returns right after
        else:
            yield frame(cursor, event)
    await _await_terminal_row(executions, run_id)  # best effort: finish_run catch-up
    if last_frame is not None:
        yield last_frame


__all__ = ["router"]
