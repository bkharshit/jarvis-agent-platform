"""Shared run/stream helpers for the agent and workflow route modules (S6,
ADR 0015 §6): enqueueing a queue message, the blocking segment wait, and
the SSE framing all key on run_id only — the executor behind the message
is a `kind` parameter (D41). Private module: `agents.py` and
`workflows.py` import from here; agent-route behavior is byte-identical."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal
from uuid import uuid4

from fastapi import Depends

from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import RunRequest
from jarvis.api.sse import frame
from jarvis.config import Settings
from jarvis.domain.auth import Principal
from jarvis.domain.events import ExecutionEvent, is_pause, is_terminal
from jarvis.domain.execution import TERMINAL_STATUSES, RunResult
from jarvis.persistence.scoped import TenantScopedExecutions
from jarvis.ports.queue import RunQueueMessage
from jarvis.runtime.limits import deadline_from_now

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)

# S10 (ADR 0010): a run's segments end at a pause just like at a terminal —
# a blocking caller returns a non-terminal awaiting_input row and the client
# resumes with POST /executions/{id}/resume.
TERMINAL_SET: set[str] = set(TERMINAL_STATUSES)
SEGMENT_END_STATUSES: set[str] = TERMINAL_SET | {"awaiting_input"}


def queue_message(
    settings: Settings,
    kind: Literal["agent", "workflow"],
    resource_id: str,
    version_id: str,
    req: RunRequest,
    principal: Principal,
) -> RunQueueMessage:
    """Everything a worker needs to execute this run without consulting the
    requester again; the deadline is absolute so it survives the cross-process
    hop (ADR 0008 §1). The principal rides along for tenant-scoped model
    resolution (stored credentials) — the queue is a trust boundary (ADR
    0008), so workers may trust it. Workflow messages (D41) stamp
    `kind="workflow"` on the message AND `metadata.kind` on the row; agent
    messages keep their byte-identical shape (no kind in metadata)."""
    metadata = dict(req.metadata)
    if kind == "workflow":
        metadata["kind"] = "workflow"
    return RunQueueMessage(
        run_id=str(uuid4()),
        kind=kind,
        agent_id=resource_id,
        agent_version_id=version_id,
        tenant_id=principal.tenant_id,
        principal=principal,
        input=req.input,
        session_id=req.session_id,
        user_id=req.user_id,
        trace_id=str(uuid4()),
        metadata=metadata,
        variables=dict(req.variables),
        deadline=deadline_from_now(settings.run_timeout_seconds),
    )


def queued_result(message: RunQueueMessage) -> RunResult:
    """The execution row as it exists at enqueue time (`status='queued'`)."""
    return RunResult(
        run_id=message.run_id,
        agent_id=message.agent_id,
        status="queued",
        input=message.input,
        agent_version_id=message.agent_version_id,
        tenant_id=message.tenant_id,
        session_id=message.session_id,
        trace_id=message.trace_id,
        metadata=dict(message.metadata),
    )


async def await_segment(
    container: AppContainer,
    executions: TenantScopedExecutions,
    run_id: str,
    after: int | None,
    *,
    end_on_pause_status: bool = True,
) -> RunResult:
    """Wait for the run's current segment to end (a terminal or a pause),
    then return the row once it carries that state. `after` attaches the
    stream beyond an already-durable segment end (the resume route passes
    the pause frame's cursor) — and the resume path must not treat the OLD
    pause's row status as a stream end: an empty first batch there means
    the resumed segment hasn't emitted yet, so it waits (the S10
    pause-again race — without this the blocking resume 500'd)."""
    held: ExecutionEvent | None = None
    async for _cursor, event in container.streams.subscribe(
        run_id, after, end_on_pause_status=end_on_pause_status
    ):
        held = event  # subscribe returns right after the terminal/pause
    # The row write trails the last event; poll until it matches. Nothing
    # streamed at all means the resume was absorbed (the run moved on
    # between the 409 check and the enqueue) — only a terminal can be true.
    statuses = TERMINAL_SET if held is None else SEGMENT_END_STATUSES
    row = await await_row_status(executions, run_id, statuses)
    if row is None:
        raise ApiError(500, "internal", f"run {run_id!r} never reached a segment end")
    return row


async def await_row_status(
    executions: TenantScopedExecutions, run_id: str, statuses: set[str]
) -> RunResult | None:
    """The last event lands moments before finish_run — poll the row until
    it reaches one of `statuses` (bounded; the stream already guaranteed
    the event). `executions` is the caller's (tenant-scoped) execution view."""
    run = await executions.get(run_id)
    for _ in range(100):
        if run is not None and run.status in statuses:
            return run
        await asyncio.sleep(0.05)
        run = await executions.get(run_id)
    return None


async def queue_stream(
    container: AppContainer,
    executions: TenantScopedExecutions,
    run_id: str,
    last_cursor: int | None,
) -> AsyncIterator[str]:
    """Frame every event; hold the segment-end frame (terminal or pause,
    S10) back until the run row carries that state, so a stream that ends
    carries the run's final state (the web UI fetches the detail or fires
    the resume immediately after the stream closes)."""
    last_frame: str | None = None
    async for cursor, event in container.streams.subscribe(run_id, last_cursor):
        if is_terminal(event) or is_pause(event):
            last_frame = frame(cursor, event)  # subscribe returns right after
        else:
            yield frame(cursor, event)
    await await_row_status(executions, run_id, SEGMENT_END_STATUSES)  # finish/pause catch-up
    if last_frame is not None:
        yield last_frame


__all__ = [
    "SEGMENT_END_STATUSES",
    "TERMINAL_SET",
    "ContainerDep",
    "await_row_status",
    "await_segment",
    "queue_message",
    "queue_stream",
    "queued_result",
]
