"""Evaluation tables (S11, ADR 0017 §2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15

`eval_datasets` / `eval_runs` / `eval_results` — the scoring layer over
ordinary runs (D48). Cases and scorers are JSONB on the dataset (no
eval_cases table); eval_runs snapshots the dataset (D1) and pins an
agent version; eval_results reference the child agent_executions rows
eagerly (run_ids exist before enqueue). eval_results carry no tenant —
scoping joins through the eval_runs parent (D29).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_datasets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default="default",
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("cases", JSONB(), nullable=False),
        sa.Column("scorers", JSONB(), nullable=False),
        sa.Column("judge_model", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eval_datasets_tenant_id", "eval_datasets", ["tenant_id"])

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenants.id"),
            nullable=False,
            server_default="default",
        ),
        sa.Column("dataset_id", sa.String(), sa.ForeignKey("eval_datasets.id"), nullable=False),
        sa.Column("dataset", JSONB(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("agent_version_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_eval_runs_tenant_id", "eval_runs", ["tenant_id"])
    op.create_index("ix_eval_runs_dataset_id", "eval_runs", ["dataset_id"])
    op.create_index("ix_eval_runs_agent_id", "eval_runs", ["agent_id"])

    op.create_table(
        "eval_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("eval_run_id", sa.String(), sa.ForeignKey("eval_runs.id"), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("scores", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("eval_run_id", "case_id"),
    )
    op.create_index("ix_eval_results_eval_run_id", "eval_results", ["eval_run_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_results_eval_run_id", table_name="eval_results")
    op.drop_table("eval_results")
    op.drop_index("ix_eval_runs_agent_id", table_name="eval_runs")
    op.drop_index("ix_eval_runs_dataset_id", table_name="eval_runs")
    op.drop_index("ix_eval_runs_tenant_id", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_eval_datasets_tenant_id", "eval_datasets")
    op.drop_table("eval_datasets")
