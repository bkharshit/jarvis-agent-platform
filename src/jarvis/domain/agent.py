"""Agent definitions and immutable version snapshots — pure Pydantic, no IO."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(_Model):
    """Reference to a model. `api_key_env` names an environment variable —
    the secret itself never enters the domain."""

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None


class ToolBinding(_Model):
    name: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class MemoryConfig(_Model):
    enabled: bool = False
    max_messages: int = Field(default=20, ge=1, le=200)
    session_key: str | None = None


class StrategyConfig(_Model):
    type: Literal["function_calling", "react"]
    params: dict[str, Any] = Field(default_factory=dict)


class AgentDefinition(_Model):
    id: str
    name: str = Field(min_length=1)
    description: str = ""
    model: ModelRef
    system_prompt: str = ""
    user_prompt_template: str | None = None
    tools: list[ToolBinding] = Field(default_factory=list)
    strategy: StrategyConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    max_iterations: int = Field(default=8, ge=1, le=32)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    output_schema: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent name must not be blank")
        return value

    def enabled_tools(self) -> list[ToolBinding]:
        return [binding for binding in self.tools if binding.enabled]


class AgentVersion(_Model):
    """Immutable, append-only snapshot of an AgentDefinition.

    `snapshot` is the replay source of truth for every execution that
    references this version (ADR 0002: snapshot-vs-reference).
    """

    id: str
    agent_id: str
    version: int = Field(ge=1)
    snapshot: AgentDefinition
    label: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))