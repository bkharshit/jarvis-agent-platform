"""Agents CRUD + blocking run + SSE stream (plan §5, ADR 0008 runs).

Thin controllers: shape requests into domain calls, map domain outcomes onto
HTTP. Every run goes through the queue — the API persists a queued row + a
queue message in one transaction and streams the run's events out of the
database (PgEventStream), so runs survive this process. Everything else
(versioning, limits, events, persistence) lives in the container. The run/
stream helpers live in `_run_routes` (S6): the same machinery serves
workflow runs with a `kind` parameter (D41)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.routes._run_routes import (
    ContainerDep,
)
from jarvis.api.routes._run_routes import (
    await_segment as _await_segment,
)
from jarvis.api.routes._run_routes import (
    queue_message as _queue_message,
)
from jarvis.api.routes._run_routes import (
    queue_stream as _queue_stream,
)
from jarvis.api.routes._run_routes import (
    queued_result as _queued_result,
)
from jarvis.api.schemas import (
    AgentDetail,
    AgentList,
    AgentUpsertRequest,
    RunRequest,
    VersionSummary,
)
from jarvis.api.sse import SSE_HEADERS, parse_last_event_id
from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.execution import RunResult

router = APIRouter(prefix="/agents", tags=["agents"])

__all__ = ["router"]


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


def _validate_strategy_type(container: AppContainer, strategy_type: str | None) -> None:
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
    message = _queue_message(
        container.settings, "agent", definition.id, version.id, req, auth.principal
    )
    await auth.executions.create_queued_run(_queued_result(message), message)
    return await _await_segment(container, auth.executions, message.run_id, None)


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
        message = _queue_message(
            container.settings, "agent", definition.id, version.id, req, auth.principal
        )
        # Row + message land atomically; subscribe replays anything the
        # worker emitted before we attached.
        await auth.executions.create_queued_run(_queued_result(message), message)
        generator = _queue_stream(container, auth.executions, message.run_id, last_cursor)

    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)
