"""Agent definitions and immutable version snapshots — pure Pydantic, no IO."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvCredentialRef(_Model):
    """Credential reference: the *name* of an environment variable — the
    secret itself never enters the domain (D18, ADR 0005)."""

    type: Literal["env"]
    env_var: str


class StoredCredentialRef(_Model):
    """Credential reference: a stored (BYOK) credential id, tenant-scoped at
    resolution time (ADR 0006). An id alone is never authorization."""

    type: Literal["stored"]
    credential_id: str


CredentialRef = Annotated[EnvCredentialRef | StoredCredentialRef, Field(discriminator="type")]


class ModelRef(_Model):
    """Reference to a model. `credential_ref` is a *reference* (env-var name
    or stored credential id) — credential material never enters the domain,
    a snapshot, or an API response (ADR 0006 §1)."""

    provider: str
    model: str
    base_url: str | None = None
    credential_ref: CredentialRef | None = None


class ToolBinding(_Model):
    name: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class MemoryConfig(_Model):
    enabled: bool = False
    max_messages: int = Field(default=20, ge=1, le=200)
    session_key: str | None = None
    # D45: "window" is exactly today's flat last-N slice (byte-identical,
    # the default for every pre-S12 snapshot); "summarize" compacts the
    # evicted prefix into a rolling conversation summary (ADR 0016 §2).
    strategy: Literal["window", "summarize"] = "window"


class ConversationMemoryState(_Model):
    """The rolling-summary state persisted on a conversation row (S12,
    ADR 0016 §2). `summarized_count` is a COUNT of leading conversation
    messages — per-conversation sequences are gapless from 1, so the count
    is the sequence boundary of the summarized prefix."""

    summary: str | None = None
    summarized_count: int = Field(default=0, ge=0)


class ScratchpadEntry(_Model):
    """One working-memory key-value row (S12, ADR 0016 §3, D46) — keyed
    (agent_id, session_id, key); `value` is an opaque string the model
    round-trips through the memory_* builtin tools."""

    agent_id: str
    session_id: str
    key: str
    value: str
    updated_at: datetime


class StrategyConfig(_Model):
    # D36: `type` is a plain string — the closed Literal lived here only
    # while the registry was closed twice. Typo protection now lives at the
    # create boundary (API 422 / CLI error, against the live registry); the
    # resolve boundary terminal-fails a snapshot whose strategy is gone
    # (error_kind="strategy"). Existing snapshots and YAML stay valid.
    type: str = Field(min_length=1)
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
