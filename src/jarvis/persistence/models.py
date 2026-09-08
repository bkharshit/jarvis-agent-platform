"""SQLAlchemy 2.x table mappings (ADR 0001, ADR 0002).

Typed Pydantic-over-JSONB: domain objects serialize to JSONB columns at the
repository boundary — never raw strings (ADR 0002). `agent_versions.snapshot`
is the immutable replay source of truth; `agents` is a mutable pointer row.
`execution_events.cursor` is a global monotonic BIGSERIAL — the SSE
Last-Event-ID — with a per-run gapless `sequence` beside it (ADR 0003).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# `awaiting_input` is a pause, not terminal (S10, ADR 0010 §2) — it does not
# join TERMINAL_STATUSES and the run row can still reach one.
EXECUTION_STATUSES = (
    "queued",
    "running",
    "awaiting_input",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
)
MESSAGE_ROLES = ("system", "developer", "user", "assistant", "tool")
QUEUE_STATUSES = ("pending", "claimed", "done")
USER_ROLES = ("owner", "admin", "member")
DEFAULT_TENANT = "default"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    """Mutable pointer row; the definition lives in version snapshots."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # NULL = platform-shared (visible to every tenant, ADR 0009 §4);
    # single-tenant/anonymous agents are shared.
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class AgentVersionRow(Base):
    """Immutable append-only snapshot of a full AgentDefinition."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class AgentExecutionRow(Base):
    """One agent run; id is the run_id (ADR 0003: every execution has an id)."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        Index("ix_agent_executions_agent_created", "agent_id", "created_at"),
        Index("ix_agent_executions_session", "session_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_version_id: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("tenants.id"),
        nullable=False,
        default=DEFAULT_TENANT,
        server_default=DEFAULT_TENANT,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        Enum(*EXECUTION_STATUSES, name="execution_status", native_enum=True),
        nullable=False,
        default="running",
    )
    input: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    total_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_cursor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The pause deadline (S10, ADR 0010 §2): set on awaiting_input, NULL
    # otherwise, cleared on resume/finish — the sweeper's reaper scans it.
    awaiting_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class RunQueueRow(Base):
    """One durable queue message per queued run (ADR 0008).

    `run_id` is unique — a run is enqueued exactly once; `payload` holds
    the full `RunQueueMessage` so a worker never consults the requester.
    A claim holds a lease (`lease_until`); the sweeper reaps expired ones.
    """

    __tablename__ = "run_queue"
    __table_args__ = (Index("ix_run_queue_status_id", "status", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(*QUEUE_STATUSES, name="run_queue_status", native_enum=True),
        nullable=False,
        default="pending",
    )
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class RunCancelRow(Base):
    """A cross-process cancellation *request* (ADR 0008 §6): the owning
    worker's heartbeat pops it and triggers the cooperative token."""

    __tablename__ = "run_cancels"

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # scrypt hash string (stdlib, format-versioned); NULL = keys-only user.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SessionRow(Base):
    """Server-side session (ADR 0009 §2): only the token hash is stored —
    revocation is a DELETE, expiry a column."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class ApiKeyRow(Base):
    """API key metadata. Only the SHA-256 hash and a display prefix are
    stored — plaintext is returned exactly once at creation."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CredentialRow(Base):
    """Stored (BYOK) credential. `ciphertext` is the AES-GCM envelope
    {v, key_id, nonce, ct} — there is no plaintext column to leak
    (ADR 0006 §7)."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    ciphertext: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpServerRow(Base):
    """One configured MCP server (S4, ADR 0012). Name is the immutable join
    key from agent version snapshots; NULL tenant = platform-shared."""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        # Postgres treats NULLs as distinct, so "unique per tenant, unique
        # among shared" needs two partial indexes (the 0007 pattern).
        Index(
            "uq_mcp_servers_owned_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
        Index(
            "uq_mcp_servers_shared_name",
            "name",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("agent_id", "session_id", name="uq_conversation"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("tenants.id"),
        nullable=False,
        default=DEFAULT_TENANT,
        server_default=DEFAULT_TENANT,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class MessageRow(Base):
    """One row per persisted message.

    Conversation-scoped rows carry (conversation_id, sequence) — the gapless
    per-conversation history; execution-scoped rows (the run transcript)
    carry execution_id only. The same logical message may appear in both
    views when memory is enabled.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True
    )
    execution_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    role: Mapped[str] = mapped_column(
        Enum(*MESSAGE_ROLES, name="message_role", native_enum=True), nullable=False
    )
    content: Mapped[dict[str, Any] | str] = mapped_column(JSONB, nullable=False)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_conversation_sequence"),
    )


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tool_call_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class ExecutionEventRow(Base):
    """Global BIGSERIAL cursor = SSE Last-Event-ID (ADR 0003)."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("execution_id", "sequence", name="uq_execution_sequence"),)

    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "AgentExecutionRow",
    "AgentRow",
    "AgentVersionRow",
    "ApiKeyRow",
    "Base",
    "ConversationRow",
    "CredentialRow",
    "DEFAULT_TENANT",
    "ExecutionEventRow",
    "McpServerRow",
    "MessageRow",
    "RunCancelRow",
    "RunQueueRow",
    "SessionRow",
    "TenantRow",
    "ToolExecutionRow",
    "USER_ROLES",
    "UserRow",
]
