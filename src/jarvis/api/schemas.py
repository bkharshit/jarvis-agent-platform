"""Request/response models for the /v1 API (plan §5).

Create/patch payloads are partial (server assigns id + timestamps and owns
versioning); responses reuse domain models directly so the API never
re-invents them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.agent import (
    AgentDefinition,
    MemoryConfig,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import RunResult
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolResult


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(_Model):
    """Body for /run and /stream. `run_id` is stream-resume only: attach to
    an existing run's stream instead of starting a new one."""

    input: str = Field(min_length=1)
    session_id: str | None = None
    user_id: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class AgentUpsertRequest(_Model):
    """Partial create/update payload — everything except name/model/strategy
    is optional and falls back to the AgentDefinition defaults."""

    name: str | None = None
    description: str | None = None
    model: ModelRef | None = None
    system_prompt: str | None = None
    user_prompt_template: str | None = None
    tools: list[ToolBinding] | None = None
    strategy: StrategyConfig | None = None
    memory: MemoryConfig | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=32)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    output_schema: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("agent name must not be blank")
        return value


class VersionSummary(_Model):
    version: int
    id: str
    label: str
    created_at: datetime


class AgentDetail(_Model):
    definition: AgentDefinition
    versions: list[VersionSummary]


class AgentList(_Model):
    items: list[AgentDefinition]


class CursorEvent(_Model):
    """One replayed event tagged with its durable cursor (SSE Last-Event-ID
    space), so JSON replay can drive exactly-once clients too."""

    cursor: int
    event: ExecutionEvent


class EventList(_Model):
    run_id: str
    after: int | None = None
    events: list[CursorEvent]


class ExecutionDetail(_Model):
    run: RunResult
    messages: list[Message]
    tool_executions: list[ToolResult]


class ExecutionList(_Model):
    items: list[RunResult]


class MessageList(_Model):
    agent_id: str
    session_id: str
    messages: list[Message]


class CancelResult(_Model):
    run_id: str
    cancelled: bool
    status: str


class SectionCapability(_Model):
    """One IA section's enablement fact (F1). Disabled sections always carry
    the `stage` that will enable them; `detail` carries derived facts
    (registries mirror registries — never hardcoded)."""

    enabled: bool
    mode: str | None = None
    summary: str | None = None
    stage: str | None = None
    detail: dict[str, Any] | None = None


class CapabilitiesResponse(_Model):
    sections: dict[str, SectionCapability]


__all__ = [
    "AgentDetail",
    "AgentList",
    "AgentUpsertRequest",
    "CancelResult",
    "CapabilitiesResponse",
    "CursorEvent",
    "EventList",
    "ExecutionDetail",
    "ExecutionList",
    "MessageList",
    "RunRequest",
    "SectionCapability",
    "VersionSummary",
]
