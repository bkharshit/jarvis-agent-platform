"""Executions: list, detail, cancel (idempotent), resume (S10), event replay.

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
from jarvis.api.routes.agents import (
    SEGMENT_END_STATUSES,
    ContainerDep,
    _await_row_status,
    _await_segment,
)
from jarvis.api.schemas import (
    CancelResult,
    CursorEvent,
    EventList,
    ExecutionDetail,
    ExecutionList,
    LlmTraceEntry,
    LlmTraceResponse,
    ResumeBody,
)
from jarvis.api.sse import SSE_HEADERS, frame
from jarvis.domain.events import is_pause, is_terminal
from jarvis.domain.execution import TERMINAL_STATUSES, ExecutionStatus, RunResult
from jarvis.persistence.scoped import TenantScopedExecutions
from jarvis.runtime.worker import finish_paused_run, worker_persist

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


@router.get("/{run_id}/llm-trace", response_model=LlmTraceResponse)
async def get_llm_trace(
    run_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> LlmTraceResponse:
    """The web-readable half of the JARVIS_LLM_TRACE debug trace (ADR 0014):
    the process-local buffer holding the actual model request/response per
    call. Empty entries is a 200, never an error — flag off, backend
    restarted since the run, or the run executed in a separate worker
    process (distributed mode keeps the log as its only trace)."""
    await _require_run(auth.executions, run_id)
    raw = container.runtime.llm_trace_buffer.get(run_id)
    return LlmTraceResponse(run_id=run_id, entries=[LlmTraceEntry(**entry) for entry in raw])


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> CancelResult:
    """Idempotent. A run live in THIS process gets its runtime token; a
    queued or foreign-worker run gets a cross-process cancel request
    (ADR 0008 §6) that the owning worker's heartbeat pops. A paused run
    (S10) is finished directly — no heartbeat holds it — on the same
    append-terminal-and-finish path the reaper uses, so cancelling an
    awaiting_input run is immediate, not cooperative. A finished run is a
    no-op that reports its current status."""
    run = await _require_run(auth.executions, run_id)
    if run.status in TERMINAL_STATUSES:
        return CancelResult(run_id=run_id, cancelled=False, status=run.status)
    if run.status == "running" and container.runtime.cancel(run_id):
        return CancelResult(run_id=run_id, cancelled=True, status=run.status)
    if run.status == "awaiting_input":
        finished = await finish_paused_run(
            container.executions,
            container.queue,
            worker_persist(container.executions, container.notifier),
            run_id,
            "cancelled by user",
        )
        if not finished:
            # Raced with a resume claim — the run is live again; the
            # cooperative path still applies.
            await container.queue.request_cancel(run_id, "cancelled by user")
        row = await auth.executions.get(run_id)
        return CancelResult(
            run_id=run_id,
            cancelled=finished,
            status=row.status if row is not None else run.status,
        )
    await container.queue.request_cancel(run_id, "cancelled by user")
    return CancelResult(run_id=run_id, cancelled=True, status=run.status)


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: str,
    req: ResumeBody,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> RunResult:
    """Human-in-the-loop resume (S10, ADR 0010 §4): merge the answer into
    the paused run's queue payload and block until the resumed segment ends
    (like /run). A segment can pause again — the route then returns the
    still-awaiting row and the client answers the next question."""
    run = await _require_run(auth.executions, run_id)
    if run.status != "awaiting_input":
        raise ApiError(
            409, "conflict", f"execution {run_id!r} is {run.status!r}, not awaiting input"
        )
    # Attach the stream beyond the current pause frame, so the wait covers
    # only the segment the worker is about to run — the already-durable
    # pause would otherwise end the stream immediately. The old pause's row
    # status must not end the wait either (the resumed segment may not have
    # emitted yet, and it may pause again — the route then returns THAT row).
    last = await auth.executions.latest_event(run_id)
    after = last[0] if last is not None else None
    await container.queue.enqueue_resume(run_id, req.to_domain())
    return await _await_segment(
        container, auth.executions, run_id, after, end_on_pause_status=False
    )


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
    """Replay from `after`, then live-tail until the run's segment ends
    (a finished run's stream is a pure replay — the terminal ends it; a
    paused run's stream ends at the pause frame, S10). The segment-end frame
    is held back until the run row carries that state, mirroring the stream
    route, so stream end carries the run's final state."""
    last_frame: str | None = None
    async for cursor, event in container.streams.subscribe(run_id, after):
        if is_terminal(event) or is_pause(event):
            last_frame = frame(cursor, event)  # subscribe returns right after
        else:
            yield frame(cursor, event)
    await _await_row_status(executions, run_id, SEGMENT_END_STATUSES)
    if last_frame is not None:
        yield last_frame


__all__ = ["router"]
