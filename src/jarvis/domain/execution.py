"""Execution state: statuses, cancellation, run context and results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.auth import Principal
from jarvis.domain.message import Usage


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# `awaiting_input` is a pause, not terminal (S10, ADR 0010 §2) — it never
# joins TERMINAL_STATUSES; a paused run still reaches exactly one terminal.
ExecutionStatus = Literal[
    "queued", "running", "awaiting_input", "succeeded", "failed", "cancelled", "timed_out"
]

# Statuses a run row can no longer leave (the terminal event's counterpart
# on the run row itself). Used by subscribers deciding whether a run whose
# replay came up empty has actually finished.
TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled", "timed_out")


class ExecutionCancelled(Exception):
    """Raised when a run observes its cancellation token.

    The orchestrator catches this and emits `run.cancelled` — it never
    escapes a run.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CancellationToken:
    """Cooperative cancellation shared by the orchestrator, the model
    adapter and every tool execution (plain class wrapping asyncio.Event)."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None
        self._children: list[CancellationToken] = []

    @property
    def triggered(self) -> bool:
        return self._event.is_set()

    def trigger(self, reason: str = "cancelled") -> None:
        if self.reason is None:
            self.reason = reason
        self._event.set()
        for child in self._children:
            child.trigger(reason)

    def raise_if_triggered(self) -> None:
        if self.triggered:
            raise ExecutionCancelled(self.reason or "cancelled")

    async def wait(self) -> None:
        await self._event.wait()

    def child(self) -> CancellationToken:
        """Derive a token that fires when the parent fires (and may also be
        triggered independently, e.g. by a per-tool timeout)."""
        child = CancellationToken()
        if self.triggered:
            child.trigger(self.reason or "cancelled")
        else:
            self._children.append(child)
        return child


@dataclass
class ExecutionContext:
    """Everything a run knows about itself. Caller-identity fields
    (user_id/metadata) anticipate auth/multi-tenancy without carrying it."""

    run_id: str
    agent_id: str
    agent_version_id: str
    tenant_id: str | None = None  # stamped from the principal (S2, ADR 0009 §6)
    principal: Principal | None = None  # for principal-aware model resolution
    session_id: str | None = None
    user_id: str | None = None
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    cancel: CancellationToken = field(default_factory=CancellationToken)
    deadline: datetime | None = None
    iteration: int = 0
    usage: Usage = field(default_factory=Usage)
    variables: dict[str, Any] = field(default_factory=dict)
    temperature: float | None = None  # from the agent definition
    # Structured-output context, set by the orchestrator from the agent
    # definition + provider capabilities before the first step.
    output_schema: dict[str, Any] | None = None
    structured_mode: str = "json_schema"  # none | json_mode | json_schema

    def check_limits(self) -> None:
        """Raise if cancelled or past the deadline."""
        self.cancel.raise_if_triggered()
        if self.deadline is not None and datetime.now(UTC) >= self.deadline:
            raise ExecutionCancelled("deadline exceeded")


class RunResult(_Model):
    run_id: str
    agent_id: str
    status: ExecutionStatus
    input: str = ""
    agent_version_id: str = ""
    # Owning tenant, stamped from the principal at enqueue time and carried
    # through the worker's terminal writes (S2, ADR 0009 §6).
    tenant_id: str | None = None
    session_id: str | None = None
    trace_id: str = ""
    final_message: str | None = None
    total_usage: Usage = Field(default_factory=Usage)
    iterations: int = 0
    error: str | None = None
    error_kind: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    event_cursor: int | None = None
    # Row metadata (D41): a workflow run stamps {"kind": "workflow"} so
    # listings distinguish the sibling executor's rows. Empty for agents.
    metadata: dict[str, Any] = Field(default_factory=dict)
