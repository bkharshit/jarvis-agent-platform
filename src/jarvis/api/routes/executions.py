"""Executions: list, detail, cancel (idempotent), event replay (plan §5).

`GET /{id}/events` returns JSON replay by default or an SSE stream when the
client sends `Accept: text/event-stream` — same cursor space as the
Last-Event-ID either way (ADR 0003)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.routes.agents import ContainerDep, _live_stream
from jarvis.api.schemas import (
    CancelResult,
    CursorEvent,
    EventList,
    ExecutionDetail,
    ExecutionList,
)
from jarvis.api.sse import SSE_HEADERS, frame
from jarvis.domain.execution import ExecutionStatus, RunResult

router = APIRouter(prefix="/executions", tags=["executions"])


async def _require_run(container: AppContainer, run_id: str) -> RunResult:
    run = await container.executions.get(run_id)
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
    container: AppContainer = ContainerDep,
) -> ExecutionList:
    items = await container.executions.list_runs(
        agent_id=agent_id,
        status=status,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )
    return ExecutionList(items=items)


@router.get("/{run_id}")
async def get_execution(
    run_id: str, container: AppContainer = ContainerDep
) -> ExecutionDetail:
    run = await _require_run(container, run_id)
    messages = await container.executions.list_messages(run_id)
    tool_executions = await container.executions.list_tool_executions(run_id)
    return ExecutionDetail(run=run, messages=messages, tool_executions=tool_executions)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, container: AppContainer = ContainerDep) -> CancelResult:
    """Idempotent: triggers the live run's token; a finished run is a no-op
    that reports its current status."""
    run = await _require_run(container, run_id)
    if run.status != "running":
        return CancelResult(run_id=run_id, cancelled=False, status=run.status)
    triggered = container.runtime.cancel(run_id)
    return CancelResult(run_id=run_id, cancelled=triggered, status=run.status)


@router.get("/{run_id}/events", response_model=None)
async def list_events(
    run_id: str,
    request: Request,
    after: int | None = None,
    container: AppContainer = ContainerDep,
) -> EventList | StreamingResponse:
    await _require_run(container, run_id)  # 404 when unknown
    if "text/event-stream" in request.headers.get("accept", ""):
        sink = container.bus.get(run_id)
        if sink is not None:
            generator: AsyncIterator[str] = _live_stream(sink, after)
        else:
            generator = _replay_frames(container, run_id, after)
        return StreamingResponse(
            generator, media_type="text/event-stream", headers=SSE_HEADERS
        )
    pairs = [
        CursorEvent(cursor=cursor, event=event)
        async for cursor, event in container.executions.replay_with_cursor(run_id, after)
    ]
    return EventList(run_id=run_id, after=after, events=pairs)


async def _replay_frames(
    container: AppContainer, run_id: str, after: int | None
) -> AsyncIterator[str]:
    async for cursor, event in container.executions.replay_with_cursor(run_id, after):
        yield frame(cursor, event)


__all__ = ["router"]
