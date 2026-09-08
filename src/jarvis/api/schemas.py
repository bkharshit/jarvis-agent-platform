"""Request/response models for the /v1 API (plan §5).

Create/patch payloads are partial (server assigns id + timestamps and owns
versioning); responses reuse domain models directly so the API never
re-invents them."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.domain.agent import (
    AgentDefinition,
    MemoryConfig,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import RunResult
from jarvis.domain.mcp import MCP_NAME_SLUG, McpServer, McpServerConfig
from jarvis.domain.message import Message
from jarvis.domain.tools import ToolDescriptor, ToolResult
from jarvis.ports.queue import ResumeRequest


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


class ResumeBody(_Model):
    """Body for POST /executions/{id}/resume (S10, ADR 0010 §4; ADR 0011):
    an ask_human pause answers with `content`; a tool-approval pause answers
    with `tool_approval` (batch shorthand) or `decisions` (per-call
    verdicts — calls absent from the map are declined, ADR 0011). Exactly
    one is present."""

    content: str | None = None
    tool_approval: bool | None = None
    decisions: dict[str, bool] | None = None

    @model_validator(mode="after")
    def _exactly_one_answer(self) -> ResumeBody:
        given = [
            present
            for present in (
                self.content is not None,
                self.tool_approval is not None,
                self.decisions is not None,
            )
            if present
        ]
        if len(given) != 1:
            raise ValueError("provide exactly one of `content`, `tool_approval`, or `decisions`")
        if self.content is not None and not self.content.strip():
            raise ValueError("content must not be blank")
        if self.decisions is not None and not self.decisions:
            raise ValueError("decisions must not be empty")
        return self

    def to_domain(self) -> ResumeRequest:
        if self.content is not None:
            return ResumeRequest(kind="content", content=self.content)
        if self.tool_approval is not None:
            return ResumeRequest(kind="tool_approval", approved=self.tool_approval)
        assert self.decisions is not None  # validator guarantees one variant
        return ResumeRequest(kind="decisions", decisions=self.decisions)


class LoginRequest(_Model):
    """Body for /auth/login. Plain string email — validation parity with
    members invites, no email-validator dependency."""

    email: str
    password: str = Field(min_length=1)


class WhoamiResponse(_Model):
    """GET /auth/whoami — the acting principal. `mode` mirrors
    domain.auth.AuthMode; `email`/`display_name` are None/"" for the
    anonymous principal."""

    tenant_id: str
    mode: str
    user_id: str | None = None
    email: str | None = None
    display_name: str = ""
    role: str | None = None


class MemberCreate(_Model):
    """POST /members. `password` is optional — a keys-only member carries no
    password hash and cannot log in until one is set."""

    email: str
    password: str | None = None
    display_name: str = ""
    role: str = "member"


class MemberPatch(_Model):
    display_name: str | None = None
    role: str | None = None


class MemberOut(_Model):
    """A member of the tenant — deliberately no password_hash field; the
    scrypt string never crosses the API."""

    id: str
    tenant_id: str
    email: str
    display_name: str = ""
    role: str
    created_at: datetime | None = None


class MemberList(_Model):
    items: list[MemberOut]


class ApiKeyCreate(_Model):
    name: str = Field(min_length=1)


class ApiKeyOut(_Model):
    """Metadata only — no plaintext, no hash; `key_prefix` is display form."""

    id: str
    tenant_id: str
    user_id: str
    name: str
    key_prefix: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyList(_Model):
    items: list[ApiKeyOut]


class ApiKeyCreated(_Model):
    """Create-time response: the ONLY response that ever carries the
    plaintext key (`plaintext`), alongside its stored metadata."""

    id: str
    name: str
    key_prefix: str
    plaintext: str
    created_at: datetime | None = None


class CredentialCreate(_Model):
    """POST /credentials — `secret` is the BYOK key material. It is
    encrypted server-side and never stored or returned in plaintext
    (ADR 0006 §7)."""

    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    secret: str = Field(min_length=1)


class CredentialPatch(_Model):
    name: str | None = None
    secret: str | None = None


class CredentialOut(_Model):
    """Metadata only — NO ciphertext, NO secret. The AES-GCM envelope never
    crosses the API (ADR 0006 §7)."""

    id: str
    tenant_id: str
    name: str
    provider: str
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    revoked_at: datetime | None = None


class CredentialList(_Model):
    items: list[CredentialOut]


class McpServerCreate(_Model):
    """POST /mcp/servers — config carries env-var NAMES only (ADR 0005);
    the values resolve from the process env at connect time."""

    name: str = Field(min_length=1)
    config: McpServerConfig
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        if not re.fullmatch(MCP_NAME_SLUG, value):
            raise ValueError(
                f"server name must be a slug matching {MCP_NAME_SLUG} (got {value!r})"
                " — it becomes part of every tool name mcp__<name>__<tool>"
            )
        return value


class McpServerPatch(_Model):
    """PATCH /mcp/servers/{id} — `enabled` and `config` only. A patch that
    carries `name` is a 422: the name is the join key from agent version
    snapshots (D37), immutable once created."""

    enabled: bool | None = None
    config: McpServerConfig | None = None
    name: str | None = None


class McpServerList(_Model):
    """GET /mcp/servers — responses reuse the domain model directly."""

    items: list[McpServer]


class McpProbeResponse(_Model):
    """POST /mcp/servers/{id}/probe — a fresh connect + tools/list, no
    persistence, no registry side effects (D37 §4)."""

    server: McpServer
    tools: list[ToolDescriptor]


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


class ModelListResponse(_Model):
    """Live catalog from a provider endpoint (ADR 0007) — what the endpoint
    answered, not a configured fact."""

    provider: str
    base_url: str | None = None
    models: list[str]


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
    "ModelListResponse",
    "ResumeBody",
    "RunRequest",
    "SectionCapability",
    "VersionSummary",
]
