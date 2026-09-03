"""Agents CRUD + blocking run (plan §5).

Thin controllers: shape requests into domain calls, map domain outcomes onto
HTTP. Everything else (versioning, limits, events, persistence) lives in the
container."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends
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
from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, AgentVersion
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.runtime.limits import deadline_from_now

router = APIRouter(prefix="/agents", tags=["agents"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)


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


__all__ = ["router"]
