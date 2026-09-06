"""Agent strategy protocols — Protocols ONLY.

ADR 0004: a strategy performs AT MOST ONE model invocation per `step` call
and never loops; the orchestrator owns limits, cancellation and terminal
events.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.agent import StrategyConfig
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient


class StepOutcome(BaseModel):
    """What one strategy step produced: pending tool calls, or a finish.

    `messages` carries strategy-requested appends (e.g. a ReAct format
    correction); the orchestrator appends them after the assistant message."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # "tool_calls" | "finish" | "ask_human"
    assistant_message: Message
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    messages: list[Message] = Field(default_factory=list)


class ToolCallsStep(StepOutcome):
    kind: str = "tool_calls"


class FinishStep(StepOutcome):
    kind: str = "finish"
    finish_reason: str = "stop"


class AskHumanStep(StepOutcome):
    """The strategy wants a human answer before it can continue (S10, ADR
    0010 §3.2). The orchestrator persists the assistant message and pauses
    the run with `reason="strategy"`; the answer arrives as a resume
    content message. Sibling of FinishStep/ToolCallsStep."""

    kind: str = "ask_human"
    question: str = ""


@runtime_checkable
class AgentStrategy(Protocol):
    """One step of an agent loop. May stream `text.delta` / `model.*` events
    via the sink; may NOT loop, emit terminal events, or execute tools."""

    @property
    def name(self) -> str: ...

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome: ...


class StrategyRegistry(Protocol):
    """Explicit registry — resolves a StrategyConfig to a strategy.
    Raises on unknown strategy type."""

    def resolve(self, config: StrategyConfig) -> AgentStrategy: ...


__all__ = [
    "AgentStrategy",
    "AskHumanStep",
    "FinishStep",
    "StepOutcome",
    "StrategyRegistry",
    "ToolCallsStep",
]
