"""Span table (S7, ADR 0021 §7, D77)

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-24

`spans` — finished spans for the Observability section. `run_id` is a
LOOSE reference (no FK — the D57 provenance pattern: a run row is not
deleted in v1, and spans must never couple run lifecycle to storage).
`trace_id` is the run's persisted trace_id (D73) — the grouping key for
the UI and the OTel trace id verbatim. `kind`/`status` are plain
strings validated by the domain type — new kinds are data, not a
schema break (ADR 0021 §7).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default="default",
        ),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("span_id", sa.String(length=32), nullable=False),
        sa.Column("parent_span_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_message", sa.Text(), nullable=True),
        sa.Column("attributes", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spans_trace_id", "spans", ["trace_id"])
    op.create_index("ix_spans_run_id", "spans", ["run_id"])
    op.create_index("ix_spans_tenant_started", "spans", ["tenant_id", "started_at"])


def downgrade() -> None:
    op.drop_table("spans")
