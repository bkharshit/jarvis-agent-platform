"""Workflows CRUD + publish + blocking run + SSE stream (S6, ADR 0015 §6).

The exact agents shape one level up: CRUD + append-only versions, a queued
run/stream through the SHARED `_run_routes` helpers with `kind="workflow"`
(D41), and publish-time agent-node pinning (D42). Publish lints (stale
pins, ignored memory, unreachable nodes) ride every definition response as
warnings — never failures. Tenancy identical to agents (owner tenant
stamps, NULL = shared; a foreign tenant's workflow is simply not found)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
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
    RunRequest,
    VersionSummary,
    WorkflowDetail,
    WorkflowList,
    WorkflowUpsertRequest,
)
from jarvis.api.sse import SSE_HEADERS, parse_last_event_id
from jarvis.domain.execution import RunResult
from jarvis.domain.workflow import (
    AgentNodeConfig,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowVersion,
    lint_workflow,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])

__all__ = ["router"]


async def _require_definition(auth: AuthContext, workflow_id: str) -> WorkflowDefinition:
    definition = await auth.workflows.get(workflow_id)
    if definition is None:
        raise ApiError(404, "not_found", f"workflow {workflow_id!r} not found")
    return definition


async def _require_version(auth: AuthContext, workflow_id: str) -> WorkflowVersion:
    version = await auth.workflows.latest_version(workflow_id)
    if version is None:
        raise ApiError(404, "not_found", f"workflow {workflow_id!r} has no published version")
    return version


def _definition_from_create(req: WorkflowUpsertRequest, workflow_id: str) -> WorkflowDefinition:
    try:
        return WorkflowDefinition(id=workflow_id, **req.model_dump())
    except ValidationError as exc:
        raise _graph_422(exc) from None


def _definition_from_patch(
    req: WorkflowUpsertRequest, definition: WorkflowDefinition
) -> WorkflowDefinition:
    try:
        return WorkflowDefinition.model_validate(
            {**definition.model_dump(), **req.model_dump(), "updated_at": datetime.now(UTC)}
        )
    except ValidationError as exc:
        raise _graph_422(exc) from None


def _graph_422(exc: ValidationError) -> ApiError:
    """Graph validation problems as field-addressable 422s (ADR 0015 §1).
    Only the JSON-serializable slice of each pydantic error survives: raw
    `ctx` entries can carry ValueError objects (not encodable in the
    response)."""
    raw = exc.errors()
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in raw]
    return ApiError(
        422,
        "validation",
        "; ".join(str(e["msg"]) for e in raw),
        details={"errors": errors},
    )


async def _pins_and_lints(
    container: AppContainer, auth: AuthContext, definition: WorkflowDefinition
) -> list[str]:
    """Publish lints (ADR 0015 §6) — stale agent pins (a newer version
    exists), memory-enabled node agents (node agents run memory-less in v1),
    unreachable nodes. Warnings, never failures."""
    stale: list[str] = []
    ignored: list[str] = []
    for node in definition.nodes:
        if node.type != "agent" or not isinstance(node.config, AgentNodeConfig):
            continue
        latest = await auth.agents.latest_version(node.config.agent_id)
        if latest is not None and node.config.agent_version_id != latest.id:
            stale.append(node.id)
        elif latest is None:
            stale.append(node.id)  # the pinned agent has no published version at all
        else:
            memory_on = latest.snapshot.memory.enabled
            if memory_on:
                ignored.append(node.id)
    return lint_workflow(
        definition,
        stale_pins=stale,
        memory_ignored=ignored,
    )


async def _pin_agent_versions(
    container: AppContainer, auth: AuthContext, definition: WorkflowDefinition
) -> WorkflowDefinition:
    """D42: freeze each agent node's `agent_version_id` to the agent's
    LATEST published version at publish time — the snapshot's meaning never
    changes under it. An agent node whose agent has no published version is
    a 422, not a lint: publishing an unrunnable version would fail honestly
    at run time instead."""
    pinned_nodes: list[WorkflowNode] = []
    for node in definition.nodes:
        if node.type == "agent" and isinstance(node.config, AgentNodeConfig):
            version = await auth.agents.latest_version(node.config.agent_id)
            if version is None:
                raise ApiError(
                    422,
                    "validation",
                    f"node {node.id!r} pins agent {node.config.agent_id!r}, "
                    "which has no published version",
                )
            pinned_nodes.append(
                node.model_copy(
                    update={
                        "config": node.config.model_copy(update={"agent_version_id": version.id})
                    }
                )
            )
        else:
            pinned_nodes.append(node)
    # model_copy skips re-validation — rebuild so the snapshot holds typed
    # nodes, not raw dicts.
    return WorkflowDefinition.model_validate({**definition.model_dump(), "nodes": pinned_nodes})


async def _detail(
    container: AppContainer, auth: AuthContext, definition: WorkflowDefinition
) -> WorkflowDetail:
    versions = await auth.workflows.list_versions(definition.id)
    lints = await _pins_and_lints(container, auth, definition)
    return WorkflowDetail(
        definition=definition,
        versions=[
            VersionSummary(version=v.version, id=v.id, label=v.label, created_at=v.created_at)
            for v in versions
        ],
        lints=lints,
    )


# --- CRUD -------------------------------------------------------------------


@router.post("", status_code=201)
async def create_workflow(
    req: WorkflowUpsertRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> WorkflowDetail:
    definition = _definition_from_create(req, workflow_id=str(uuid4()))
    definition = await _pin_agent_versions(container, auth, definition)
    try:
        await auth.workflows.create(definition)
    except IntegrityError:
        raise ApiError(
            409, "conflict", f"workflow name {definition.name!r} already exists"
        ) from None
    return await _detail(container, auth, definition)


@router.get("")
async def list_workflows(
    limit: int = 50, offset: int = 0, auth: AuthContext = AuthDep
) -> WorkflowList:
    items = await auth.workflows.list_workflows(limit=limit, offset=offset)
    return WorkflowList(items=items)


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    request: Request,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> WorkflowDetail:
    definition = await _require_definition(auth, workflow_id)
    return await _detail(container, auth, definition)


# --- runs (queued — the same machinery as agents, kind="workflow") ------------


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    req: RunRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> RunResult:
    """Blocking run — enqueue with kind="workflow" (D41), then wait for the
    segment to end. A pause INSIDE an agent node (S10 composition, ADR 0015
    §7) ends the segment like an agent pause: the route returns the
    awaiting_input row and the client resumes with
    POST /executions/{id}/resume."""
    definition = await _require_definition(auth, workflow_id)
    version = await _require_version(auth, workflow_id)
    message = _queue_message(
        container.settings, "workflow", definition.id, version.id, req, auth.principal
    )
    await auth.executions.create_queued_run(_queued_result(message), message)
    return await _await_segment(container, auth.executions, message.run_id, None)


@router.post("/{workflow_id}/stream")
async def stream_workflow(
    workflow_id: str,
    req: RunRequest,
    request: Request,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> StreamingResponse:
    """SSE run: enqueue with kind="workflow", then frame every event the
    worker writes — including `node.*` events stamped with the executing
    node's node_id (D43). Resume with `Last-Event-ID` plus `run_id`, exactly
    like the agents stream."""
    try:
        last_cursor = parse_last_event_id(request.headers.get("last-event-id"))
    except ValueError as exc:
        raise ApiError(400, "validation", f"invalid Last-Event-ID: {exc}") from None

    if req.run_id is not None:
        run = await auth.executions.get(req.run_id)
        if run is None or run.agent_id != workflow_id:
            raise ApiError(404, "not_found", f"execution {req.run_id!r} not found")
        generator = _queue_stream(container, auth.executions, req.run_id, last_cursor)
    else:
        definition = await _require_definition(auth, workflow_id)
        version = await _require_version(auth, workflow_id)
        message = _queue_message(
            container.settings, "workflow", definition.id, version.id, req, auth.principal
        )
        await auth.executions.create_queued_run(_queued_result(message), message)
        generator = _queue_stream(container, auth.executions, message.run_id, last_cursor)

    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{workflow_id}/versions/{version}")
async def get_workflow_version(
    workflow_id: str, version: int, auth: AuthContext = AuthDep
) -> WorkflowVersion:
    await _require_definition(auth, workflow_id)
    snapshot = await auth.workflows.get_version(workflow_id, version)
    if snapshot is None:
        raise ApiError(404, "not_found", f"version {version} of workflow {workflow_id!r} not found")
    return snapshot


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    req: WorkflowUpsertRequest,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> WorkflowDetail:
    definition = await _require_definition(auth, workflow_id)
    definition = _definition_from_patch(req, definition)
    # D42: publish pins agent nodes to their agents' latest versions.
    pinned = await _pin_agent_versions(container, auth, definition)
    try:
        version = await auth.workflows.update_and_publish(pinned)
    except IntegrityError:
        raise ApiError(
            409, "conflict", f"workflow name {definition.name!r} already exists"
        ) from None
    return await _detail(container, auth, version.snapshot)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, auth: AuthContext = AuthDep) -> None:
    await _require_definition(auth, workflow_id)
    deleted = await auth.workflows.delete(workflow_id)
    if not deleted:
        raise ApiError(409, "conflict", f"workflow {workflow_id!r} has executions; delete refused")
