"""mcp_servers: configured MCP server registry (S4, ADR 0012)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-08

One new table; no existing table or snapshot changes meaning (agents bind
MCP tools by name, `mcp__<server>__<tool>`, and ToolBinding is unchanged).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(), nullable=False),
        # NULL = platform-shared (visible to every tenant, ADR 0009 §4).
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_servers_tenant_id", "mcp_servers", ["tenant_id"], unique=False)
    # Name uniqueness across the owned/shared split: Postgres treats NULLs
    # as distinct, so one composite unique index cannot enforce "unique per
    # tenant AND unique among shared" — two partial indexes do.
    op.create_index(
        "uq_mcp_servers_owned_name",
        "mcp_servers",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "uq_mcp_servers_shared_name",
        "mcp_servers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_mcp_servers_shared_name", table_name="mcp_servers")
    op.drop_index("uq_mcp_servers_owned_name", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_tenant_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
