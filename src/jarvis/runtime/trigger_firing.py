"""Trigger firing composition (S13, ADR 0019 §2, D58) — a trigger is an
ordinary queue producer. The fired run is the child-run shape with no
parent: fresh run_id, fresh trace_id, a fresh platform deadline (there is
no parent to inherit), a synthesized `mode="system"` principal (D28 needs
a tenant to resolve BYOK refs; `None` would break it, and reusing
`api_key`/`anonymous` would be dishonest), and the `metadata["trigger"]`
provenance namespace the executions filter and the event-observer depth
guard both read. The UNCHANGED worker claims and executes it — a
trigger-fired run is indistinguishable from a manual one in
`/v1/executions`, exactly like eval children.

Firing failures raise `TriggerFireError` and the caller surfaces them its
own way (cron → `last_error`, webhook → 500, event → log): never a retry
storm, and the finalizing/firing run is never affected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from jarvis.domain.agent import AgentVersion
from jarvis.domain.auth import Principal
from jarvis.domain.trigger import Trigger
from jarvis.domain.workflow import render_template
from jarvis.runtime.child_runs import QueuedRunWriter, child_message, enqueue_child_run
from jarvis.runtime.limits import deadline_from_now

__all__ = ["TriggerFireError", "TriggerFireService", "TriggerVersionResolver"]


class TriggerFireError(Exception):
    """Firing failed honestly — the target agent is gone, has no published
    version, or the pinned id is unresolvable."""


class TriggerVersionResolver(Protocol):
    """Structural view of the agent repo (`SqlAgentRepo` satisfies it): the
    frozen-snapshot loader a server-stamped pin resolves through, plus the
    tenant-scoped latest published version `latest` policy resolves at fire
    time (a foreign agent reads as absent — the D29 pattern)."""

    async def latest_version(
        self, agent_id: str, *, tenant_id: str | None = None
    ) -> AgentVersion | None: ...

    async def get_version_by_id(self, version_id: str) -> AgentVersion | None: ...


def _default_input(trigger: Trigger, values: dict[str, Any]) -> str:
    """The honest per-kind default for an EMPTY template (ADR 0019 §2)."""
    if trigger.kind == "cron":
        return (
            f"Scheduled run of agent {trigger.agent_id} by trigger "
            f"'{trigger.name}' at {values['now']}."
        )
    if trigger.kind == "webhook":
        payload = values.get("payload")
        # A manual fire carries no payload — the run still gets an input.
        return payload if isinstance(payload, str) and payload else "{}"
    return (
        f"Run {values.get('run_id')} of agent {values.get('agent_id')} "
        f"finished with status {values.get('status')}."
    )


class TriggerFireService:
    """Composes one fired run per trigger. Stateless — build one per
    container and call `fire` from the scheduler, the webhook route, and
    the event observer alike."""

    def __init__(
        self,
        *,
        executions: QueuedRunWriter,
        agents: TriggerVersionResolver,
        run_timeout_seconds: float | None,
    ) -> None:
        self._executions = executions
        self._agents = agents
        self._run_timeout_seconds = run_timeout_seconds

    async def fire(
        self, trigger: Trigger, *, context_vars: dict[str, Any], incoming_depth: int = 0
    ) -> str:
        """Resolve the target version, render the input, enqueue the run,
        and return its pre-generated run_id. `incoming_depth` is 0 for
        cron/webhook (a schedule/webhook is a root) and the finalized
        run's own depth for event triggers — the fired run carries
        `incoming + 1` (§6's guard reads it)."""
        version = await self._resolve_version(trigger)
        now = datetime.now(UTC)
        values: dict[str, Any] = {
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "now": now.isoformat(),
            **context_vars,
        }
        input_text = (
            render_template(trigger.input_template, values)
            if trigger.input_template
            else _default_input(trigger, values)
        )
        message = child_message(
            agent_id=trigger.agent_id,
            agent_version_id=version.id,
            input=input_text,
            tenant_id=trigger.tenant_id,
            principal=Principal(tenant_id=trigger.tenant_id, mode="system"),
            session_id=None,
            trace_id=uuid4().hex,  # D73: 32-hex = a valid OTel trace id
            deadline=deadline_from_now(self._run_timeout_seconds),
            metadata={
                "trigger": {
                    "trigger_id": trigger.id,
                    "kind": trigger.kind,
                    "name": trigger.name,
                    "depth": incoming_depth + 1,
                }
            },
        )
        return await enqueue_child_run(executions=self._executions, message=message)

    async def _resolve_version(self, trigger: Trigger) -> AgentVersion:
        """`pinned` replays the route-stamped version forever (D1); `latest`
        resolves the target's current published version at fire time — the
        explicit opt-in to drift. Both fail honestly when unresolvable."""
        if trigger.version_policy == "pinned":
            pin = trigger.agent_version_id
            if not pin:
                raise TriggerFireError(
                    f"trigger {trigger.name!r} is pinned but carries no "
                    "agent_version_id — re-save it with a published target"
                )
            version = await self._agents.get_version_by_id(pin)
            if version is None:
                raise TriggerFireError(f"trigger {trigger.name!r} pinned version {pin!r} not found")
            if version.agent_id != trigger.agent_id:
                raise TriggerFireError(
                    f"trigger {trigger.name!r} pinned version {pin!r} belongs to agent "
                    f"{version.agent_id!r}, not {trigger.agent_id!r}"
                )
            return version
        version = await self._agents.latest_version(trigger.agent_id, tenant_id=trigger.tenant_id)
        if version is None:
            raise TriggerFireError(
                f"trigger {trigger.name!r} target agent {trigger.agent_id!r} not found "
                "(or invisible to this tenant) with no published version"
            )
        return version
