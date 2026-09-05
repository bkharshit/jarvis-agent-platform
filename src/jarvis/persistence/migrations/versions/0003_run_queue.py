"""run queue + cancellation requests (S1, ADR 0008)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUEUE_STATUS = sa.Enum("pending", "claimed", "done", name="run_queue_status")


def upgrade() -> None:
    # NOTE: no explicit Enum.create() — op.create_table emits CREATE TYPE
    # itself (0001's DuplicateObjectError gotcha).
    op.create_table(
        "run_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", QUEUE_STATUS, nullable=False),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_run_queue_status_id", "run_queue", ["status", "id"], unique=False)
    op.create_table(
        "run_cancels",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("run_cancels")
    op.drop_table("run_queue")
    op.execute("DROP TYPE IF EXISTS run_queue_status")
