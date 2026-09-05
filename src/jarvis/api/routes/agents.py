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
from jarvis.domain.events import is_terminal
from jarvis.domain.execution import RunResult
from jarvis.ports.queue import RunQueueMessage
from jarvis.runtime.limits import deadline_from_now

router = APIRouter(prefix="/agents", tags=["agents"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)

# Statuses a run row can no longer leave.
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}


async def _require_definition(container: AppContainer, agent_id: str) -> AgentDefinition:
    definition = await container.agents.get(agent_id)
    if definition is None:
        raise ApiError(404, "not_found", f"agent {agent_id!r} not found")
    return definition


async def _require_version(container: AppContainer, agent_id: str) -> AgentVersion:
    version = await container.agents.latest_version(agent_id)
    if version is None:
        raise ApiError(404, "not_found", f"agent {agent_id!r} has no published version")
    return version


async def _detail(container: AppContainer, definition: AgentDefinition) -> AgentDetail:
    versions = await container.agents.list_versions(definition.id)
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


# --- CRUD -------------------------------------------------------------------


@router.post("", status_code=201)
async def create_agent(
    req: AgentUpsertRequest, container: AppContainer = ContainerDep
) -> AgentDetail:
    definition = _definition_from_create(req, agent_id=str(uuid4()))
    try:
        await container.agents.create(definition)
    except IntegrityError:
        raise ApiError(
            409, "conflict", f"agent name {definition.name!r} already exists"
        ) from None
    return await _detail(container, definition)


@router.get("")
async def list_agents(
    limit: int = 50, offset: int = 0, container: AppContainer = ContainerDep
) -> AgentList:
    items = await container.agents.list_agents(limit=limit, offset=offset)
    return AgentList(items=items)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str, container: AppContainer = ContainerDep
) -> AgentDetail:
    definition = await _require_definition(container, agent_id)
    return await _detail(container, definition)


@router.get("/{agent_id}/versions/{version}")
async def get_agent_version(
    agent_id: str, version: int, container: AppContainer = ContainerDep
) -> AgentVersion:
    await _require_definition(container, agent_id)
    snapshot = await container.agents.get_version(agent_id, version)
    if snapshot is None:
        raise ApiError(404, "not_found", f"version {version} of agent {agent_id!r} not found")
    return snapshot


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str, req: AgentUpsertRequest, container: AppContainer = ContainerDep
) -> AgentDetail:
    definition = await _require_definition(container, agent_id)
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        return await _detail(container, definition)
    updated = definition.model_copy(update={**payload, "updated_at": datetime.now(UTC)})
    try:
        version = await container.agents.update_and_publish(updated)
    except IntegrityError:
        raise ApiError(409, "conflict", f"agent name {updated.name!r} already exists") from None
    return await _detail(container, version.snapshot)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str, container: AppContainer = ContainerDep
) -> None:
    await _require_definition(container, agent_id)
    deleted = await container.agents.delete(agent_id)
    if not deleted:
        raise ApiError(
            409, "conflict", f"agent {agent_id!r} has executions; delete refused"
        )


# --- runs (queued — ADR 0008: the queue is the only execution path) ------------


def _queue_message(
    settings: Settings, definition: AgentDefinition, version: AgentVersion, req: RunRequest
) -> RunQueueMessage:
    """Everything a worker needs to execute this run without consulting the
    requester again; the deadline is absolute so it survives the cross-process
    hop (ADR 0008 §1)."""
    return RunQueueMessage(
        run_id=str(uuid4()),
        agent_id=definition.id,
        agent_version_id=version.id,
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
        session_id=message.session_id,
        trace_id=message.trace_id,
    )


@router.post("/{agent_id}/run")
async def run_agent(
    agent_id: str, req: RunRequest, container: AppContainer = ContainerDep
) -> RunResult:
    """Blocking run — enqueue, then wait for the worker's terminal event.
    The subscribe replays anything the worker already wrote, so there is no
    race between enqueueing and listening."""
    definition = await _require_definition(container, agent_id)
    version = await _require_version(container, agent_id)
    message = _queue_message(container.settings, definition, version, req)
    await container.executions.create_queued_run(_queued_result(message), message)
    async for _cursor, _event in container.streams.subscribe(message.run_id):
        pass  # the stream ends exactly at the terminal event
    run = await _await_terminal_row(container, message.run_id)
    if run is None:
        raise ApiError(
            500, "internal", f"run {message.run_id!r} never reached a terminal state"
        )
    return run


async def _await_terminal_row(container: AppContainer, run_id: str) -> RunResult | None:
    """The terminal event lands moments before finish_run — poll the row
    until it is terminal (bounded; the stream already guaranteed the event)."""
    run = await container.executions.get(run_id)
    for _ in range(100):
        if run is not None and run.status in _TERMINAL_STATUSES:
            return run
        await asyncio.sleep(0.05)
        run = await container.executions.get(run_id)
    return None


@router.post("/{agent_id}/stream")
async def stream_agent(
    agent_id: str,
    req: RunRequest,
    request: Request,
    container: AppContainer = ContainerDep,
) -> StreamingResponse:
    """SSE run: enqueue, then frame every event the worker writes. Resume with
    `Last-Event-ID` (durable cursor) plus `run_id` in the body — replay and
    live tail are the same cursor space (PgEventStream tails the DB, so the
    stream survives this process). Ends with the run's single terminal event."""
    try:
        last_cursor = parse_last_event_id(request.headers.get("last-event-id"))
    except ValueError as exc:
        raise ApiError(400, "validation", f"invalid Last-Event-ID: {exc}") from None

    if req.run_id is not None:
        run = await container.executions.get(req.run_id)
        if run is None or run.agent_id != agent_id:
            raise ApiError(404, "not_found", f"execution {req.run_id!r} not found")
        generator = _queue_stream(container, req.run_id, last_cursor)
    else:
        definition = await _require_definition(container, agent_id)
        version = await _require_version(container, agent_id)
        message = _queue_message(container.settings, definition, version, req)
        # Row + message land atomically; subscribe replays anything the
        # worker emitted before we attached.
        await container.executions.create_queued_run(_queued_result(message), message)
        generator = _queue_stream(container, message.run_id, last_cursor)

    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


async def _queue_stream(
    container: AppContainer, run_id: str, last_cursor: int | None
) -> AsyncIterator[str]:
    """Frame every event; hold the terminal frame back until the run row is
    terminal, so a stream that ends carries the run's final state (the web
    UI fetches the detail immediately after the stream closes)."""
    last_frame: str | None = None
    async for cursor, event in container.streams.subscribe(run_id, last_cursor):
        if is_terminal(event):
            last_frame = frame(cursor, event)  # subscribe returns right after
        else:
            yield frame(cursor, event)
    await _await_terminal_row(container, run_id)  # best effort: finish_run catch-up
    if last_frame is not None:
        yield last_frame


__all__ = ["router"]
