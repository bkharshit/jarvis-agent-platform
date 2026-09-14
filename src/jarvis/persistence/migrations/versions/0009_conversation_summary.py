"""Conversation rolling-summary state (S12, ADR 0016 §2)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-14

`conversations` gains the compaction state of the summarize strategy
(D45): `summary` is the rolling summary text, `summarized_count` is how
many LEADING conversation messages it covers (per-conversation sequences
are gapless from 1, so the count is the sequence boundary). Both default
to "nothing summarized" — every existing conversation is a window
conversation until a summarize-strategy run compacts it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("summarized_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "summarized_count")
    op.drop_column("conversations", "summary")
