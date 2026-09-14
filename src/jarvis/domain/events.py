"""Execution events: shared envelope, discriminated union, terminal semantics.

The event log is the source of truth for what happened during a run
(ADR 0003). Invariants — gapless per-run sequence, exactly-one-terminal
event, nothing after the terminal event — are enforced by the sink and
unit-tested against the helpers in this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.message import ToolCall, Usage


class _Event(BaseModel):
    """Shared envelope fields (ADR 0003). `sequence` is per-run, gapless,
    sink-assigned — unassigned (None) until the sink stamps it.

    `node_id` (S6, ADR 0015 §3, D43): the workflow node that emitted the
    event, stamped by the node's sink wrapper. None for plain agent-run
    events and old persisted rows — backward compatible in both
    directions."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: str
    sequence: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    node_id: str | None = None


# --- lifecycle -----------------------------------------------------------


class RunStarted(_Event):
    type: Literal["run.started"] = "run.started"
    agent_id: str
    agent_version_id: str
    session_id: str | None = None
    input: str = ""


class IterationStarted(_Event):
    type: Literal["iteration.started"] = "iteration.started"
    iteration: int


class IterationCompleted(_Event):
    type: Literal["iteration.completed"] = "iteration.completed"
    iteration: int
    usage: Usage


# --- model invocations -----------------------------------------------------


class ModelInvocationStarted(_Event):
    type: Literal["model.invocation.started"] = "model.invocation.started"
    attempt: int = 1


class TextDelta(_Event):
    type: Literal["text.delta"] = "text.delta"
    text: str


class ModelInvocationCompleted(_Event):
    type: Literal["model.invocation.completed"] = "model.invocation.completed"
    usage: Usage
    finish_reason: str
    model: str


# --- tool calls ------------------------------------------------------------


class ToolCallRequested(_Event):
    type: Literal["tool.call.requested"] = "tool.call.requested"
    tool_call_id: str
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ToolCallStarted(_Event):
    type: Literal["tool.call.started"] = "tool.call.started"
    tool_call_id: str
    name: str


class ToolCallCompleted(_Event):
    type: Literal["tool.call.completed"] = "tool.call.completed"
    tool_call_id: str
    name: str
    output: str = ""
    is_error: bool = False
    latency_ms: int = 0


class ToolCallFailed(_Event):
    type: Literal["tool.call.failed"] = "tool.call.failed"
    tool_call_id: str
    name: str
    error: str
    kind: Literal["validation", "timeout", "internal"] = "internal"


class ToolCallDeclined(_Event):
    """The human declined this call (S10/ADR 0011 §3) — records the
    *decision*, not an execution: no started/completed ever follow, no
    tool_executions row exists. The refusal tool message in the transcript
    is the model-facing copy; this event is the auditable one."""

    type: Literal["tool.call.declined"] = "tool.call.declined"
    tool_call_id: str
    name: str


# --- workflow nodes (S6, ADR 0015 §3, D43) -----------------------------------


class NodeStarted(_Event):
    """A workflow node began executing. `node_id` overrides the envelope's
    optional field with a required one — the event IS the node boundary
    marker; there is no `node.failed` (D43: an inner failure surfaces as
    the run's terminal `run.failed` naming the node)."""

    type: Literal["node.started"] = "node.started"
    node_id: str
    node_type: Literal["agent", "tool", "condition"]


class NodeCompleted(_Event):
    """A workflow node finished; `output` carries the agent's final message
    or the tool's result (never an inner terminal event)."""

    type: Literal["node.completed"] = "node.completed"
    node_id: str
    node_type: Literal["agent", "tool", "condition"]
    output: str = ""
    is_error: bool = False


# --- pause (non-terminal; the segment ends here, ADR 0010 §1) ----------------


class RunAwaitingInput(_Event):
    """The run paused for a human decision. NOT terminal — exactly-one-
    terminal still holds for the whole run; the pause ends one *segment* of
    the gapless sequence (no non-terminal event may follow it within a
    segment; the next event belongs to the resumed segment)."""

    type: Literal["run.awaiting_input"] = "run.awaiting_input"
    reason: Literal["tool_approval", "strategy"]
    question: str = ""  # strategy-asked prompt text
    pending_calls: list[ToolCall] = Field(default_factory=list)  # approval-gated calls
    awaiting_until: datetime  # the pause deadline (row column carries it too)


# --- terminal (exactly one per run) -----------------------------------------


class RunCompleted(_Event):
    type: Literal["run.completed"] = "run.completed"
    final_message: str
    total_usage: Usage
    iterations: int


class RunFailed(_Event):
    type: Literal["run.failed"] = "run.failed"
    error: str
    # D36 (S3): "strategy" joins the kind set — a plugin strategy that is
    # missing (de-allow-listed/uninstalled) or that raises in `step`. Same
    # field, same envelope (ADR 0003 precedent: output_schema, timeout).
    error_kind: Literal["max_iterations", "timeout", "model", "tool", "output_schema", "strategy"]
    total_usage: Usage


class RunCancelled(_Event):
    type: Literal["run.cancelled"] = "run.cancelled"
    reason: str
    total_usage: Usage


ExecutionEvent = Annotated[
    RunStarted
    | IterationStarted
    | ModelInvocationStarted
    | TextDelta
    | ModelInvocationCompleted
    | ToolCallRequested
    | ToolCallStarted
    | ToolCallCompleted
    | ToolCallFailed
    | ToolCallDeclined
    | NodeStarted
    | NodeCompleted
    | IterationCompleted
    | RunAwaitingInput
    | RunCompleted
    | RunFailed
    | RunCancelled,
    Field(discriminator="type"),
]

TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled"})

# The one pause event — a segment end, not a terminal (ADR 0010 §1).
PAUSE_EVENT_TYPE = "run.awaiting_input"

TerminalEvent = RunCompleted | RunFailed | RunCancelled


def is_terminal(event: BaseModel) -> bool:
    return getattr(event, "type", None) in TERMINAL_EVENT_TYPES


def is_pause(event: BaseModel) -> bool:
    """True for `run.awaiting_input` — a segment end, never a terminal."""
    return getattr(event, "type", None) == PAUSE_EVENT_TYPE


class EventSequenceError(Exception):
    """Raised when a list of events violates the envelope invariants."""


def validate_event_sequence(events: list[BaseModel]) -> None:
    """Assert the envelope invariants over a completed event list:
    gapless per-run sequence starting at 0, exactly one terminal event,
    and no non-terminal event after it."""
    if not events:
        return
    run_id = events[0].run_id  # type: ignore[attr-defined]
    terminal_index: int | None = None
    for index, event in enumerate(events):
        if event.run_id != run_id:  # type: ignore[attr-defined]
            raise EventSequenceError(
                f"event {index} has run_id {event.run_id!r}, expected {run_id!r}"  # type: ignore[attr-defined]
            )
        sequence = event.sequence  # type: ignore[attr-defined]
        if sequence != index:
            raise EventSequenceError(
                f"sequence not gapless: position {index} has sequence {sequence!r}"
            )
        if is_terminal(event):
            if terminal_index is not None:
                raise EventSequenceError("more than one terminal event")
            terminal_index = index
    if terminal_index is None:
        raise EventSequenceError("no terminal event")
    if terminal_index != len(events) - 1:
        raise EventSequenceError("non-terminal event after terminal event")
