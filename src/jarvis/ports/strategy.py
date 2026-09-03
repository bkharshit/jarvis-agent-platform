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
    """What one strategy step produced: pending tool calls, or a finish."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # "tool_calls" | "finish"
    assistant_message: Message
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None


class ToolCallsStep(StepOutcome):
    kind: str = "tool_calls"


class FinishStep(StepOutcome):
    kind: str = "finish"
    finish_reason: str = "stop"


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
    "FinishStep",
    "StepOutcome",
    "StrategyRegistry",
    "ToolCallsStep",
]
