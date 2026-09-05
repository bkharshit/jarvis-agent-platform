"""execution_status gains 'queued' (S1: runs are enqueued before a worker
claims them)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block —
    # alembic runs migrations in one by default, hence the autocommit block.
    # (Different from 0001's create_type gotcha, same enum family.)
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE execution_status ADD VALUE IF NOT EXISTS 'queued'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; a true downgrade would have to
    # rebuild the type. Anything left 'queued' must move out first.
    op.execute("UPDATE agent_executions SET status = 'failed' WHERE status = 'queued'")
