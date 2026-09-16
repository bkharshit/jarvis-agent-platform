"""Trigger management (S13, ADR 0019 §6, D62) — the MCP CRUD pattern:
tenant-scoped rows, owner/admin-manage + member-read, a foreign tenant's
id is 404 (no existence leak, D29). The webhook secret is generated
server-side, sealed AES-GCM, and its plaintext returned EXACTLY ONCE
(create / rotate) — never in any GET, never logged (D29/D30). DELETE is
always allowed (no-FK provenance: past runs keep their trigger_id and
render "deleted trigger" honestly); the eval-style 409-with-runs was
rejected. A disabled trigger fires nothing — fire-now is a 409."""

from __future__ import annotations

import secrets as _secrets
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import (
    TriggerCreate,
    TriggerCreated,
    TriggerDetail,
    TriggerFireResponse,
    TriggerList,
    TriggerOut,
    TriggerPatch,
    TriggerSecretResponse,
)
from jarvis.domain.agent import AgentVersion
from jarvis.domain.trigger import CronConfig, Trigger
from jarvis.persistence.cron import next_cron_fire, validate_cron_schedule
from jarvis.runtime.trigger_firing import TriggerFireError
from jarvis.security.crypto import CryptoError, encrypt, load_master_key

router = APIRouter(prefix="/triggers", tags=["triggers"])

# Module-level Depends singleton (ruff B008).
ContainerDep = Depends(get_container)


def _require_trigger_manager(auth: AuthContext) -> None:
    """Admin/owner only — except anonymous mode, the default tenant's full
    access (the mcp role-gate pattern)."""
    if auth.user is not None and auth.principal.role not in ("owner", "admin"):
        raise ApiError(403, "forbidden", "trigger management requires the admin or owner role")


async def _visible_trigger(auth: AuthContext, container: AppContainer, trigger_id: str) -> Trigger:
    trigger = await container.triggers.get(trigger_id, tenant_id=auth.principal.tenant_id)
    if trigger is None:
        raise ApiError(404, "not_found", f"trigger {trigger_id!r} not found")
    return trigger


async def _target_version(auth: AuthContext, trigger: Trigger) -> AgentVersion | None:
    """The target agent's tenant-scoped latest published version."""
    return await auth.agents.latest_version(trigger.agent_id)


def _validate_cron(trigger: Trigger) -> None:
    if trigger.kind == "cron" and isinstance(trigger.config, CronConfig):
        try:
            validate_cron_schedule(trigger.config.schedule)
        except ValueError as exc:
            raise ApiError(422, "validation", str(exc)) from None


async def _require_published_target(auth: AuthContext, trigger: Trigger) -> AgentVersion:
    """Both policies require a published target at the boundary — a typo'd
    agent_id is a 422, not a silently never-firing row (the agent can still
    be deleted later; firing then fails honestly with last_error)."""
    version = await _target_version(auth, trigger)
    if version is None:
        raise ApiError(
            422,
            "validation",
            f"trigger target agent {trigger.agent_id!r} has no published version",
        )
    return version


def _stamp_pin(trigger: Trigger, version: AgentVersion) -> Trigger:
    if trigger.version_policy == "pinned":
        return trigger.model_copy(update={"agent_version_id": version.id})
    return trigger.model_copy(update={"agent_version_id": None})


def _stamp_next_fire(trigger: Trigger) -> Trigger:
    """A cron trigger computes its first due time at create/enable; any
    other state clears it (disabled fires nothing, other kinds never due)."""
    if trigger.kind == "cron" and trigger.enabled and isinstance(trigger.config, CronConfig):
        return trigger.model_copy(
            update={"next_fire_at": next_cron_fire(trigger.config.schedule, datetime.now(UTC))}
        )
    return trigger.model_copy(update={"next_fire_at": None})


def _seal_webhook_secret(trigger: Trigger, container: AppContainer) -> tuple[Trigger, str]:
    """Generate the HMAC secret, seal it AES-GCM into the row envelope, and
    return (trigger, plaintext-shown-once). CryptoError means the platform
    is misconfigured — the stored-credential path would fail identically."""
    plaintext = _secrets.token_urlsafe(32)
    try:
        master_key = load_master_key(container.settings)
    except CryptoError as exc:
        raise ApiError(500, "internal", str(exc)) from None
    sealed = trigger.model_copy(update={"secret": encrypt(plaintext, master_key=master_key)})
    return sealed, plaintext


async def _trigger_lints(auth: AuthContext, trigger: Trigger) -> list[str]:
    """The derived detail lints (the workflow-detail pattern): a stale pin,
    and memory-on-target — a trigger-fired run is memory-less (session_id
    is None), so a memory-enabled target silently runs without memory."""
    lints: list[str] = []
    latest = await _target_version(auth, trigger)
    if trigger.version_policy == "pinned":
        if latest is None:
            lints.append("target agent has no published version")
        elif trigger.agent_version_id != latest.id:
            lints.append("target republished since pin")
    if latest is not None and latest.snapshot.memory.enabled:
        lints.append("memory enabled on target; trigger-fired runs are memory-less")
    return lints


@router.get("")
async def list_triggers(
    auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> TriggerList:
    triggers = await container.triggers.list_triggers(tenant_id=auth.principal.tenant_id)
    return TriggerList(items=[TriggerOut.from_domain(t) for t in triggers])


@router.post("", status_code=201)
async def create_trigger(
    req: TriggerCreate, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> TriggerCreated:
    _require_trigger_manager(auth)
    try:
        trigger = Trigger(id=str(uuid4()), tenant_id=auth.principal.tenant_id, **req.model_dump())
    except ValidationError as exc:
        raise ApiError(422, "validation", exc.errors()[0]["msg"]) from None
    _validate_cron(trigger)
    version = await _require_published_target(auth, trigger)
    trigger = _stamp_pin(trigger, version)
    secret: str | None = None
    if trigger.kind == "webhook":
        trigger, secret = _seal_webhook_secret(trigger, container)
    trigger = _stamp_next_fire(trigger)
    try:
        created = await container.triggers.create(trigger, tenant_id=auth.principal.tenant_id)
    except IntegrityError:
        raise ApiError(409, "conflict", f"a trigger named {req.name!r} already exists") from None
    return TriggerCreated(trigger=TriggerOut.from_domain(created), secret=secret)


@router.get("/{trigger_id}")
async def get_trigger(
    trigger_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> TriggerDetail:
    trigger = await _visible_trigger(auth, container, trigger_id)
    return TriggerDetail(
        trigger=TriggerOut.from_domain(trigger), lints=await _trigger_lints(auth, trigger)
    )


@router.patch("/{trigger_id}")
async def update_trigger(
    trigger_id: str,
    req: TriggerPatch,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> TriggerDetail:
    """FULL document (the mcp/eval discipline). The pin re-stamps to the
    target's latest published version at patch time; enabling a cron
    recomputes next_fire_at from now, disabling clears it. The stored
    secret is untouched — the patch never carries one."""
    _require_trigger_manager(auth)
    current = await _visible_trigger(auth, container, trigger_id)
    try:
        updated = Trigger(id=current.id, tenant_id=current.tenant_id, **req.model_dump())
    except ValidationError as exc:
        raise ApiError(422, "validation", exc.errors()[0]["msg"]) from None
    _validate_cron(updated)
    version = await _require_published_target(auth, updated)
    updated = _stamp_pin(updated, version)
    updated = _stamp_next_fire(updated)
    try:
        saved = await container.triggers.update(updated, tenant_id=auth.principal.tenant_id)
    except LookupError:
        raise ApiError(404, "not_found", f"trigger {trigger_id!r} not found") from None
    return TriggerDetail(
        trigger=TriggerOut.from_domain(saved), lints=await _trigger_lints(auth, saved)
    )


@router.delete("/{trigger_id}", status_code=204)
async def delete_trigger(
    trigger_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> None:
    """Always allowed — no-FK provenance means past runs keep their
    trigger_id; the executions filter keeps working on a deleted trigger."""
    _require_trigger_manager(auth)
    deleted = await container.triggers.delete(trigger_id, tenant_id=auth.principal.tenant_id)
    if not deleted:
        raise ApiError(404, "not_found", f"trigger {trigger_id!r} not found")


@router.post("/{trigger_id}/rotate-secret")
async def rotate_webhook_secret(
    trigger_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> TriggerSecretResponse:
    _require_trigger_manager(auth)
    trigger = await _visible_trigger(auth, container, trigger_id)
    if trigger.kind != "webhook":
        raise ApiError(422, "validation", "only webhook triggers carry a secret")
    trigger, plaintext = _seal_webhook_secret(trigger, container)
    try:
        await container.triggers.update(trigger, tenant_id=auth.principal.tenant_id)
    except LookupError:
        raise ApiError(404, "not_found", f"trigger {trigger_id!r} not found") from None
    return TriggerSecretResponse(secret=plaintext)


@router.post("/{trigger_id}/fire", status_code=202)
async def fire_trigger(
    trigger_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> TriggerFireResponse:
    """Manual test-fire — the same §2 composition with the cron/webhook
    defaults. 409 when disabled: "disabled fires nothing" stays true."""
    trigger = await _visible_trigger(auth, container, trigger_id)
    if not trigger.enabled:
        raise ApiError(409, "conflict", "trigger is disabled — disabled triggers fire nothing")
    try:
        run_id = await container.firing.fire(trigger, context_vars={})
    except TriggerFireError as exc:
        raise ApiError(422, "validation", str(exc)) from None
    return TriggerFireResponse(run_id=run_id)


__all__ = ["router"]
