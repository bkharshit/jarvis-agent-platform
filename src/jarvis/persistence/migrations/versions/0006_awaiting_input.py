"""awaiting_input: human-in-the-loop pause state (S10, ADR 0010 §2)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06

The execution_status enum gains 'awaiting_input' (a pause, not terminal —
the run row can still reach exactly one terminal status) and the pause
deadline column the sweeper's reaper scans.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block —
    # alembic runs migrations in one by default, hence the autocommit block
    # (the 0002 pattern).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE execution_status ADD VALUE IF NOT EXISTS 'awaiting_input'")
    op.add_column(
        "agent_executions",
        sa.Column("awaiting_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Postgres cannot drop an enum value; a true removal would rebuild the
    # type. Paused runs must move out of 'awaiting_input' first — they have
    # no terminal event yet, so they land on the plain 'failed' terminal
    # state (nothing further can happen to them after the column is gone).
    op.execute(
        "UPDATE agent_executions SET status = 'failed',"
        " error = 'awaiting_input run interrupted by downgrade'"
        " WHERE status = 'awaiting_input'"
    )
    op.drop_column("agent_executions", "awaiting_until")
