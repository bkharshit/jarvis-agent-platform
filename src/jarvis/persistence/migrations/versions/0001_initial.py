"""initial schema: agents, versions, executions, conversations, events

Revision ID: 0001
Revises:
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXECUTION_STATUS = sa.Enum(
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    name="execution_status",
)
MESSAGE_ROLE = sa.Enum("system", "developer", "user", "assistant", "tool", name="message_role")


def upgrade() -> None:
    # NOTE: no explicit Enum.create() here — op.create_table emits CREATE TYPE
    # for the enum columns itself, and a checkfirst pre-create makes that
    # second emission fail with DuplicateObjectError.

    op.create_table(
        "agents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_version"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"], unique=False)
    op.create_table(
        "agent_executions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("agent_version_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            EXECUTION_STATUS,
            nullable=False,
            server_default="running",
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_kind", sa.String(), nullable=True),
        sa.Column("total_usage", postgresql.JSONB(), nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_cursor", sa.BigInteger(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_executions_agent_created",
        "agent_executions",
        ["agent_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_agent_executions_session", "agent_executions", ["session_id"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "session_id", name="uq_conversation"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("execution_id", sa.String(), nullable=True),
        sa.Column("role", MESSAGE_ROLE, nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("tool_call_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_sequence"),
    )
    op.create_index("ix_messages_execution_id", "messages", ["execution_id"], unique=False)
    op.create_table(
        "tool_executions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("tool_call_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_executions_execution_id", "tool_executions", ["execution_id"], unique=False
    )
    op.create_table(
        "execution_events",
        sa.Column("cursor", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cursor"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_execution_sequence"),
    )
    op.create_index(
        "ix_execution_events_execution_id", "execution_events", ["execution_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("execution_events")
    op.drop_table("tool_executions")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("agent_executions")
    op.drop_table("agent_versions")
    op.drop_table("agents")
    MESSAGE_ROLE.drop(op.get_bind(), checkfirst=True)
    EXECUTION_STATUS.drop(op.get_bind(), checkfirst=True)
