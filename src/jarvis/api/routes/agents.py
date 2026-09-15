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
from jarvis.domain.agent import AgentDefinition, AgentVersion, ToolBinding
from jarvis.domain.execution import RunResult
from jarvis.tools.subagents.provider import is_family_binding

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
        lints=await _lints(auth, definition),
    )


# --- sub-agent publish pinning (S14, ADR 0018 §1, the D42 pattern) --------------


def _family_bindings(definition: AgentDefinition) -> list[ToolBinding]:
    return [b for b in definition.tools if is_family_binding(b.name)]


async def _pin_family_bindings(
    auth: AuthContext, agent_id: str, definition: AgentDefinition
) -> AgentDefinition:
    """D51: freeze every `sub__`/`handoff__` binding's `agent_version_id` to
    the target's LATEST published version at publish time — the snapshot's
    meaning never changes under it (the `_pin_agent_versions` pattern, D42
    transposed to agents). The client NEVER supplies a pin: the route
    overwrites `config.agent_version_id` on every publish, never trusts it.
    A target with no published version — or invisible to this tenant (D29:
    the same read) — is a 422, not a lint: publishing an unrunnable snapshot
    would fail honestly at run time instead."""
    bindings = definition.tools
    if not any(is_family_binding(b.name) for b in bindings):
        return definition
    pinned: list[ToolBinding] = []
    for binding in bindings:
        config = binding.config
        if not is_family_binding(binding.name):
            pinned.append(binding)
            continue
        target_id = config.get("agent_id")
        if not isinstance(target_id, str) or not target_id:
            raise ApiError(
                422,
                "validation",
                f"tool {binding.name!r} carries no agent_id in its config — "
                "the sub-agent identity is the config's agent_id, never the name",
            )
        if target_id == agent_id:
            raise ApiError(
                422,
                "validation",
                f"tool {binding.name!r} binds agent {agent_id!r} to itself",
            )
        version = await auth.agents.latest_version(target_id)
        if version is None:
            raise ApiError(
                422,
                "validation",
                f"tool {binding.name!r} pins agent {target_id!r}, which has no "
                "published version (or is invisible to this tenant)",
            )
        pinned.append(
            binding.model_copy(update={"config": {**config, "agent_version_id": version.id}})
        )
    # model_copy skips re-validation — rebuild so the snapshot holds typed
    # bindings, not raw dicts (the workflow pinning's rebuild discipline).
    return AgentDefinition.model_validate(
        {**definition.model_dump(), "tools": [b.model_dump() for b in pinned]}
    )


async def _ensure_acyclic(auth: AuthContext, definition: AgentDefinition) -> None:
    """D51 defense-in-depth: walk the pinned sub-graph over immutable
    snapshots and 422 on a cycle. With pins always latest-at-publish and
    versions immutable the graph is acyclic by construction — this walk
    survives any future relaxation of the overwrite rule; the runtime depth
    cap (D55) is the second belt."""
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    async def visit(agent_id: str, bindings: list[ToolBinding]) -> None:
        if state.get(agent_id) == 1:
            raise ApiError(422, "validation", f"sub-agent cycle through {agent_id!r}")
        if state.get(agent_id) == 2:
            return
        state[agent_id] = 1
        for binding in bindings:
            if not is_family_binding(binding.name):
                continue
            target_id = binding.config.get("agent_id")
            if not isinstance(target_id, str) or not target_id:
                continue  # the pinning step 422s this before the walk
            pin = binding.config.get("agent_version_id")
            if isinstance(pin, str) and pin:
                version = await auth.agents.get_version_by_id(pin)
            else:
                version = await auth.agents.latest_version(target_id)
            if version is None:
                continue  # the pinned graph ends here
            await visit(target_id, version.snapshot.tools)
        state[agent_id] = 2

    await visit(definition.id, definition.tools)


async def _lints(auth: AuthContext, definition: AgentDefinition) -> list[str]:
    """Publish lints (ADR 0018 §5) — stale sub-agent pins (a newer version
    exists, or the pinned target has no published version at all) and
    memory-enabled children (children run memory-less — session_id=None).
    Warnings, never failures: versions are immutable, a stale pin still
    replays exactly what it pinned (D1)."""
    lints: list[str] = []
    for binding in _family_bindings(definition):
        name = binding.name
        target_id = binding.config.get("agent_id")
        if not isinstance(target_id, str) or not target_id:
            lints.append(f"tool {name!r} carries no agent_id in its config")
            continue
        latest = await auth.agents.latest_version(target_id)
        if latest is None:
            lints.append(
                f"tool {name!r} pins agent {target_id!r}, which has no published "
                "version (or is invisible to this tenant)"
            )
            continue
        pin = binding.config.get("agent_version_id")
        if not isinstance(pin, str) or pin != latest.id:
            lints.append(
                f"tool {name!r} pins a stale version of agent {target_id!r} — "
                "republish to pin the latest"
            )
            continue
        pinned = await auth.agents.get_version_by_id(pin)
        if pinned is not None and pinned.snapshot.memory.enabled:
            lints.append(
                f"tool {name!r} pins memory-enabled agent {target_id!r} — children "
                "run memory-less (no shared session)"
            )
    return lints


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
    definition = await _pin_family_bindings(auth, definition.id, definition)
    await _ensure_acyclic(auth, definition)
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
    updated = await _pin_family_bindings(auth, updated.id, updated)
    await _ensure_acyclic(auth, updated)
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
