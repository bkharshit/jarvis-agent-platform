"""Agents CRUD + blocking run + SSE stream (plan §5).

Thin controllers: shape requests into domain calls, map domain outcomes onto
HTTP. Everything else (versioning, limits, events, persistence) lives in the
container."""

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
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.events.bus import InProcessEventSink
from jarvis.runtime.limits import deadline_from_now

router = APIRouter(prefix="/agents", tags=["agents"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)

# Strong refs so a streamed run's asyncio.Task can't be garbage-collected
# mid-run (documented Phase 1 simplification; the sink is never dropped).
_background_tasks: set[asyncio.Task[RunResult]] = set()


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
            422, "validation", f"missing required field(s): {', '.join(missing)}"
        )
    return AgentDefinition(id=agent_id, **payload)


def _new_context(
    settings: Settings, definition: AgentDefinition, version: AgentVersion, req: RunRequest
) -> ExecutionContext:
    return ExecutionContext(
        run_id=str(uuid4()),
        agent_id=definition.id,
        agent_version_id=version.id,
        session_id=req.session_id,
        user_id=req.user_id,
        trace_id=str(uuid4()),
        metadata=dict(req.metadata),
        variables=dict(req.variables),
        deadline=deadline_from_now(settings.run_timeout_seconds),
    )


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


# --- runs ---------------------------------------------------------------------


@router.post("/{agent_id}/run")
async def run_agent(
    agent_id: str, req: RunRequest, container: AppContainer = ContainerDep
) -> RunResult:
    """Blocking run — the same AgentRuntime.run() the SSE route drives."""
    definition = await _require_definition(container, agent_id)
    version = await _require_version(container, agent_id)
    ctx = _new_context(container.settings, definition, version, req)
    return await container.runtime.run(version, req.input, ctx)


@router.post("/{agent_id}/stream")
async def stream_agent(
    agent_id: str,
    req: RunRequest,
    request: Request,
    container: AppContainer = ContainerDep,
) -> StreamingResponse:
    """SSE run: subscribe before the run starts, then frame every event.
    Resume with `Last-Event-ID` (durable cursor) plus `run_id` in the body;
    a finished run replays from the DB, a live one from its sink. The stream
    ends with the run's single terminal event."""
    try:
        last_cursor = parse_last_event_id(request.headers.get("last-event-id"))
    except ValueError as exc:
        raise ApiError(400, "validation", f"invalid Last-Event-ID: {exc}") from None

    if req.run_id is not None:
        run = await container.executions.get(req.run_id)
        if run is None or run.agent_id != agent_id:
            raise ApiError(404, "not_found", f"execution {req.run_id!r} not found")
        sink = container.bus.get(req.run_id)
        if sink is not None:
            generator = _live_stream(sink, last_cursor)
        else:
            generator = _replay_stream(container, req.run_id, last_cursor)
    else:
        definition = await _require_definition(container, agent_id)
        version = await _require_version(container, agent_id)
        ctx = _new_context(container.settings, definition, version, req)
        # get_or_create BEFORE the run task so no event can be missed.
        sink = await container.bus.get_or_create(ctx.run_id)
        task = asyncio.create_task(container.runtime.run(version, req.input, ctx, sink=sink))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        generator = _live_stream(sink, last_cursor)

    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


async def _live_stream(
    sink: InProcessEventSink, last_cursor: int | None
) -> AsyncIterator[str]:
    async for cursor, event in sink.subscribe(last_cursor):
        yield frame(cursor, event)


async def _replay_stream(
    container: AppContainer, run_id: str, last_cursor: int | None
) -> AsyncIterator[str]:
    """Finished/foreign run: cursor-space replay from the DB. If the run is
    not actually finished, the DB holds a prefix — resume then drains only
    what is persisted, so Phase 1 clients resume finished runs (live runs
    resume via the in-memory sink above)."""
    async for cursor, event in container.executions.replay_with_cursor(run_id, last_cursor):
        yield frame(cursor, event)


__all__ = ["router"]
