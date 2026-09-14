"""Working-memory scratchpad table (S12, ADR 0016 §3)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14

`memory_scratch` — the per-(agent, session) KV store the memory_*
builtin tools ride (D46). The natural key IS the primary key
(agent_id, session_id, key), so the repo's upsert conflicts on it
directly. Tenant-scoped like every table (D29); rows stamp the default
tenant unless the runtime threads ctx.tenant_id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_scratch",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default="default",
        ),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "session_id", "key"),
    )
    op.create_index("ix_memory_scratch_tenant_id", "memory_scratch", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_scratch_tenant_id", table_name="memory_scratch")
    op.drop_table("memory_scratch")
