"""Agents CRUD + blocking run + SSE stream (plan §5, ADR 0008 runs).

Thin controllers: shape requests into domain calls, map domain outcomes onto
HTTP. Every run goes through the queue — the API persists a queued row + a
queue message in one transaction and streams the run's events out of the
database (PgEventStream), so runs survive this process. Everything else
(versioning, limits, events, persistence) lives in the container."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import (
    AgentDetail,
    AgentList,
    AgentUpsertRequest,
    RunRequest,
    VersionSummary,
)
from jarvis.api.sse import SSE_HEADERS, frame, parse_last_event_id
from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.auth import Principal
from jarvis.domain.events import ExecutionEvent, is_pause, is_terminal
from jarvis.domain.execution import TERMINAL_STATUSES, RunResult
from jarvis.persistence.scoped import TenantScopedExecutions
from jarvis.ports.queue import RunQueueMessage
from jarvis.runtime.limits import deadline_from_now

router = APIRouter(prefix="/agents", tags=["agents"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)

# S10 (ADR 0010): a run's segments end at a pause just like at a terminal —
# a blocking caller returns a non-terminal awaiting_input row and the client
# resumes with POST /executions/{id}/resume.
TERMINAL_SET: set[str] = set(TERMINAL_STATUSES)
SEGMENT_END_STATUSES: set[str] = TERMINAL_SET | {"awaiting_input"}


async def _require_definition(auth: AuthContext, agent_id: str) -> AgentDefinition:
    definition = await auth.agents.get(agent_id)
    if definition is None:
        raise ApiError(404, "not_found", f"agent {agent_id!r} not found")
    return definition


async def _require_version(auth: AuthContext, agent_id: str) -> AgentVersion:
    version = await auth.agents.latest_version(agent_id)
    if version is None:
        raise ApiError(404, "not_found", f"agent {agent_id!r} has no published version")
    return version


async def _detail(auth: AuthContext, definition: AgentDefinition) -> AgentDetail:
    versions = await auth.agents.list_versions(definition.id)
    return AgentDetail(
        definition=definition,
        versions=[
            VersionSummary(version=v.version, id=v.id, label=v.label, created_at=v.created_at)
            for v in versions
        ],
    )


def _definition_from_create(req: AgentUpsertRequest, agent_id: str) -> AgentDefinition:
    payload = req.model_dump(exclude_unset=True)
    missing = sorted({"name", "model", "strategy"} - set(payload))
    if missing:
        raise ApiError(
            422,
            "validation",
            f"missing required field(s): {', '.join(missing)}",
            details={
                "errors": [
                    {"loc": ["body", field], "msg": "field required", "type": "missing"}
                    for field in missing
                ]
            },
        )
    return AgentDefinition(id=agent_id, **payload)


def _validate_strategy_type(
    container: AppContainer, strategy_type: str | None
) -> None:
    """D36: `strategy.type` is a free string in the domain; the create
    boundary validates it against the *live* registry (plugins included) so
    a typo 422s here instead of failing at run time. Update validates only
    when the payload carries a strategy — an agent pinned to a
    since-removed plugin must stay editable."""
    if strategy_type is None:
        return
    known = container.strategies.names()
    if strategy_type not in known:
        raise ApiError(
            422,
            "validation",
            f"unknown strategy type {strategy_type!r} (known: {', '.join(known)})",
            details={
                "errors": [
                    {
                        "loc": ["body", "strategy", "type"],
                        "msg": f"unknown strategy type {strategy_type!r}",
                        "type": "value_error",
                    }
                ]
            },
        )


# --- CRUD -------------------------------------------------------------------


@router.post("", status_code=201)
async def create_agent(
    req: AgentUpsertRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> AgentDetail:
    definition = _definition_from_create(req, agent_id=str(uuid4()))
    _validate_strategy_type(container, definition.strategy.type)
    try:
        await auth.agents.create(definition)
    except IntegrityError:
        raise ApiError(409, "conflict", f"agent name {definition.name!r} already exists") from None
    return await _detail(auth, definition)


@router.get("")
async def list_agents(limit: int = 50, offset: int = 0, auth: AuthContext = AuthDep) -> AgentList:
    items = await auth.agents.list_agents(limit=limit, offset=offset)
    return AgentList(items=items)


@router.get("/{agent_id}")
async def get_agent(agent_id: str, auth: AuthContext = AuthDep) -> AgentDetail:
    definition = await _require_definition(auth, agent_id)
    return await _detail(auth, definition)


@router.get("/{agent_id}/versions/{version}")
async def get_agent_version(
    agent_id: str, version: int, auth: AuthContext = AuthDep
) -> AgentVersion:
    await _require_definition(auth, agent_id)
    snapshot = await auth.agents.get_version(agent_id, version)
    if snapshot is None:
        raise ApiError(404, "not_found", f"version {version} of agent {agent_id!r} not found")
    return snapshot


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    req: AgentUpsertRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> AgentDetail:
    definition = await _require_definition(auth, agent_id)
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        return await _detail(auth, definition)
    _validate_strategy_type(container, req.strategy.type if req.strategy else None)
    # Rebuild through model_validate: model_dump deep-dumps nested models
    # (model/strategy/memory → dicts), and model_copy does not re-validate —
    # the updated definition must hold typed fields, not raw dicts.
    updated = AgentDefinition.model_validate(
        {**definition.model_dump(), **payload, "updated_at": datetime.now(UTC)}
    )
    try:
        version = await auth.agents.update_and_publish(updated)
    except IntegrityError:
        raise ApiError(409, "conflict", f"agent name {updated.name!r} already exists") from None
    return await _detail(auth, version.snapshot)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, auth: AuthContext = AuthDep) -> None:
    await _require_definition(auth, agent_id)
    deleted = await auth.agents.delete(agent_id)
    if not deleted:
        raise ApiError(409, "conflict", f"agent {agent_id!r} has executions; delete refused")


# --- runs (queued — ADR 0008: the queue is the only execution path) ------------


def _queue_message(
    settings: Settings,
    definition: AgentDefinition,
    version: AgentVersion,
    req: RunRequest,
    principal: Principal,
) -> RunQueueMessage:
    """Everything a worker needs to execute this run without consulting the
    requester again; the deadline is absolute so it survives the cross-process
    hop (ADR 0008 §1). The principal rides along for tenant-scoped model
    resolution (stored credentials) — the queue is a trust boundary (ADR
    0008), so workers may trust it."""
    return RunQueueMessage(
        run_id=str(uuid4()),
        agent_id=definition.id,
        agent_version_id=version.id,
        tenant_id=principal.tenant_id,
        principal=principal,
        input=req.input,
        session_id=req.session_id,
        user_id=req.user_id,
        trace_id=str(uuid4()),
        metadata=dict(req.metadata),
        variables=dict(req.variables),
        deadline=deadline_from_now(settings.run_timeout_seconds),
    )


def _queued_result(message: RunQueueMessage) -> RunResult:
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
    )


@router.post("/{agent_id}/run")
async def run_agent(
    agent_id: str,
    req: RunRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> RunResult:
    """Blocking run — enqueue, then wait for the worker's segment to end. The
    subscribe replays anything the worker already wrote, so there is no race
    between enqueueing and listening. A pause (S10) ends the segment like a
    terminal: the route returns the awaiting_input row and the client
    resumes with POST /executions/{id}/resume."""
    definition = await _require_definition(auth, agent_id)
    version = await _require_version(auth, agent_id)
    message = _queue_message(container.settings, definition, version, req, auth.principal)
    await auth.executions.create_queued_run(_queued_result(message), message)
    return await _await_segment(container, auth.executions, message.run_id, None)


async def _await_segment(
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
    row = await _await_row_status(executions, run_id, statuses)
    if row is None:
        raise ApiError(500, "internal", f"run {run_id!r} never reached a segment end")
    return row


async def _await_row_status(
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


@router.post("/{agent_id}/stream")
async def stream_agent(
    agent_id: str,
    req: RunRequest,
    request: Request,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> StreamingResponse:
    """SSE run: enqueue, then frame every event the worker writes. Resume with
    `Last-Event-ID` (durable cursor) plus `run_id` in the body — replay and
    live tail are the same cursor space (PgEventStream tails the DB, so the
    stream survives this process). Ends with the segment's last event
    (terminal or pause, S10): a pause closes the stream and the client
    resumes with POST /executions/{id}/resume."""
    try:
        last_cursor = parse_last_event_id(request.headers.get("last-event-id"))
    except ValueError as exc:
        raise ApiError(400, "validation", f"invalid Last-Event-ID: {exc}") from None

    if req.run_id is not None:
        run = await auth.executions.get(req.run_id)
        if run is None or run.agent_id != agent_id:
            raise ApiError(404, "not_found", f"execution {req.run_id!r} not found")
        generator = _queue_stream(container, auth.executions, req.run_id, last_cursor)
    else:
        definition = await _require_definition(auth, agent_id)
        version = await _require_version(auth, agent_id)
        message = _queue_message(container.settings, definition, version, req, auth.principal)
        # Row + message land atomically; subscribe replays anything the
        # worker emitted before we attached.
        await auth.executions.create_queued_run(_queued_result(message), message)
        generator = _queue_stream(container, auth.executions, message.run_id, last_cursor)

    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


async def _queue_stream(
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
    await _await_row_status(executions, run_id, SEGMENT_END_STATUSES)  # finish/pause catch-up
    if last_frame is not None:
        yield last_frame


__all__ = ["router"]
