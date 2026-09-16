"""Trigger rules (S13, ADR 0019 §1, D57)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-16

`triggers` — tenant-owned rows (the eval tenancy pattern), one per rule
that starts runs on its own: cron / webhook / event. `agent_id` is a loose
reference (no FK — the mcp-name precedent; firing fails honestly with
last_error when the target is gone). `config` is the kind-typed JSONB
union; `secret` is the webhook HMAC secret's AES-GCM envelope (write-only).

`agent_executions.trigger_id` — a REAL nullable column + index, NO FK: the
executions filter needs a queryable equality (the D53/D48 argument), and no
FK means a deleted trigger's past runs keep their provenance (DELETE is
always 204; the eval 409-with-runs was rejected).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_KIND = sa.Enum("cron", "webhook", "event", name="trigger_kind")


def upgrade() -> None:
    # NOTE: no explicit Enum.create() — op.create_table emits CREATE TYPE
    # itself (the D4 gotcha: a checkfirst pre-create fails with
    # DuplicateObjectError).
    op.create_table(
        "triggers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default="default",
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", TRIGGER_KIND, nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("agent_version_id", sa.String(), nullable=True),
        sa.Column("version_policy", sa.String(), nullable=False, server_default="latest"),
        sa.Column("input_template", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("secret", JSONB(), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_triggers_tenant_name"),
    )
    op.create_index("ix_triggers_tenant_id", "triggers", ["tenant_id"])
    # The scheduler's claim query (D59): kind='cron' AND enabled AND
    # next_fire_at <= now.
    op.create_index("ix_triggers_due_crons", "triggers", ["kind", "enabled", "next_fire_at"])

    op.add_column("agent_executions", sa.Column("trigger_id", sa.String(), nullable=True))
    op.create_index("ix_agent_executions_trigger_id", "agent_executions", ["trigger_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_executions_trigger_id", table_name="agent_executions")
    op.drop_column("agent_executions", "trigger_id")
    op.drop_index("ix_triggers_due_crons", table_name="triggers")
    op.drop_index("ix_triggers_tenant_id", table_name="triggers")
    op.drop_table("triggers")
    # drop_table leaves the enum behind (the 0003 precedent) — without this a
    # downgrade/upgrade cycle dies on CREATE TYPE (the D4 gotcha, again).
    op.execute("DROP TYPE IF EXISTS trigger_kind")
