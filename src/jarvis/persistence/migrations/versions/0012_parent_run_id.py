"""agent_executions gains parent_run_id (S14, ADR 0018 §3)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-15

The sub-agent/handoff parent linkage: a REAL nullable column + index, not
metadata JSONB — the cancel fan-out and the executions list filter need a
queryable equality filter, and there is no metadata filter to extend.
Backfill is unnecessary: every pre-S14 run is a root run and NULL is
exactly the "no parent" fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_executions", sa.Column("parent_run_id", sa.String(), nullable=True))
    op.create_index("ix_agent_executions_parent_run_id", "agent_executions", ["parent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_executions_parent_run_id", table_name="agent_executions")
    op.drop_column("agent_executions", "parent_run_id")
