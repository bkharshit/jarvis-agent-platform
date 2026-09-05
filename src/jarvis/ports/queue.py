"""Run queue protocol — Protocols ONLY (ADR 0008).

A run is enqueued with everything a worker needs to execute it without
consulting the requester again; `deadline` is an absolute datetime computed
at enqueue time so it survives the cross-process hop. Cancellation is
cross-process: `request_cancel` writes a request the owning worker's
heartbeat pops via `pending_cancel`. Adapters: Postgres `SKIP LOCKED`
first; Redis later behind the same protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class RunQueueMessage(BaseModel):
    """One unit of work: execute `agent_version_id` with `input`."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    agent_version_id: str
    input: str
    session_id: str | None = None
    user_id: str | None = None
    trace_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    deadline: datetime | None = None
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunQueue(Protocol):
    """Durable FIFO-ish queue of `RunQueueMessage`s with worker leases.

    `claim` hands a message to exactly one worker and starts its lease; a
    message whose lease expires may be reaped by `sweep` (requeued if the
    run emitted no events, failed otherwise)."""

    async def enqueue(self, message: RunQueueMessage) -> None:
        """Persist a message as pending."""
        ...

    async def claim(self, worker_id: str, lease: timedelta) -> RunQueueMessage | None:
        """Atomically mark one pending message claimed by `worker_id` with
        `lease`; None when the queue is empty."""
        ...

    async def ack(self, run_id: str) -> None:
        """Mark the message done after the run reached a terminal state."""
        ...

    async def renew(self, run_id: str, worker_id: str, lease: timedelta) -> bool:
        """Extend the lease iff the message is still claimed by `worker_id`.
        False means the lease was lost — the worker must stop the run."""
        ...

    async def pending_cancel(self, run_id: str) -> str | None:
        """Pop a cross-process cancellation request for `run_id`, or None."""
        ...

    async def request_cancel(self, run_id: str, reason: str) -> None:
        """Write an idempotent cross-process cancellation request (the
        owning worker's heartbeat pops it)."""
        ...

    async def sweep(self, expired_before: datetime) -> list[str]:
        """Return run_ids whose claim lease expired before `expired_before`.
        The caller decides the outcome — requeue when the run emitted no
        events, exactly-one-terminal-failure otherwise (ADR 0008 §3) — and
        acts via `requeue`/`ack`."""
        ...

    async def requeue(self, run_id: str) -> None:
        """Put a claimed message back to pending (crashed worker, no events
        emitted — safe to re-execute from scratch)."""
        ...


__all__ = ["RunQueue", "RunQueueMessage"]
