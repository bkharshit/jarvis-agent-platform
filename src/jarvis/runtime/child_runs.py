"""Child-run enqueue composition (S14, D52/D56) — the shape every child
run shares: run_id pre-generated, `create_queued_run` (row + message, one
transaction, ADR 0008 §2), everything a worker needs riding the message.
Lives below the API layer so both the eval service and the sub-agent tool
family reuse it; `api.evaluation_service` composes the same fields for
its case children (a fresh trace + the platform deadline), while the
sub-agent family shares the parent's trace and absolute deadline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from jarvis.domain.auth import Principal
from jarvis.domain.execution import RunResult
from jarvis.ports.queue import RunQueueMessage


class QueuedRunWriter(Protocol):
    """The one call the composition makes into the executions repo —
    `TenantScopedExecutions` and `SqlExecutionRepo` satisfy it
    structurally."""

    async def create_queued_run(self, result: RunResult, message: RunQueueMessage) -> None: ...


def child_message(
    *,
    kind: Literal["agent", "workflow"] = "agent",
    agent_id: str,
    agent_version_id: str,
    input: str,
    tenant_id: str | None,
    principal: Principal | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    trace_id: str,
    deadline: datetime | None,
    parent_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> RunQueueMessage:
    """Build the child's queue message. The caller owns trace_id and
    deadline — an eval child generates both (a fresh trace, the platform
    timeout); a sub-agent child shares the parent's trace and absolute
    deadline (the parent's remaining budget must bound the child, D52)."""
    return RunQueueMessage(
        run_id=str(uuid4()),
        kind=kind,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        tenant_id=tenant_id,
        principal=principal,
        input=input,
        session_id=session_id,
        user_id=user_id,
        trace_id=trace_id,
        parent_run_id=parent_run_id,
        metadata=dict(metadata or {}),
        variables=dict(variables or {}),
        deadline=deadline,
    )


async def enqueue_child_run(*, executions: QueuedRunWriter, message: RunQueueMessage) -> str:
    """Persist the child's row + message (one transaction) and return the
    pre-generated run_id. The queue enqueue itself rides
    `create_queued_run` — the eval composition (D48), unchanged."""
    await executions.create_queued_run(
        RunResult(
            run_id=message.run_id,
            agent_id=message.agent_id,
            status="queued",
            input=message.input,
            agent_version_id=message.agent_version_id,
            tenant_id=message.tenant_id,
            session_id=message.session_id,
            trace_id=message.trace_id,
            parent_run_id=message.parent_run_id,
            metadata=dict(message.metadata),
        ),
        message,
    )
    return message.run_id


def with_trigger_provenance(
    metadata: dict[str, Any], parent_metadata: dict[str, Any] | None
) -> dict[str, Any]:
    """Copy the parent's `trigger` provenance verbatim when present
    (S13, ADR 0019 §6): without the copy an event trigger on a child's
    completion re-fires through every delegation generation, the depth
    guard bypassed at each hop. Both S14 producers (sub-agent child,
    handoff successor) run their minted-fresh metadata through this — a
    parent without trigger provenance composes byte-identical children
    (regression-gated)."""
    if isinstance(parent_metadata, dict):
        trigger = parent_metadata.get("trigger")
        if isinstance(trigger, dict):
            metadata["trigger"] = dict(trigger)
    return metadata


__all__ = [
    "QueuedRunWriter",
    "child_message",
    "enqueue_child_run",
    "with_trigger_provenance",
]
