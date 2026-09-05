"""tenancy: tenants, users, sessions, api_keys, credentials + tenant columns (S2, ADR 0009)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TENANT = "default"


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed the shared default tenant before backfilling tenant_id columns
    # (the FKs below require the row to exist).
    op.execute(
        "INSERT INTO tenants (id, name, created_at) "
        f"SELECT '{DEFAULT_TENANT}', 'Default', now() WHERE NOT EXISTS "
        f"(SELECT 1 FROM tenants WHERE id = '{DEFAULT_TENANT}')"
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"], unique=False)
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"], unique=False)
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("ciphertext", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # No plaintext column exists to leak (ADR 0006 §7) — ciphertext only.
    )
    op.create_index("ix_credentials_tenant_id", "credentials", ["tenant_id"], unique=False)

    # Tenant ownership on existing tables. NULL on agents = platform-shared
    # (single-tenant agents stay visible everywhere, ADR 0009 §4).
    op.add_column("agents", sa.Column("tenant_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_agents_tenant_id", "agents", "tenants", ["tenant_id"], ["id"])
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"], unique=False)

    for table in ("agent_executions", "conversations"):
        op.add_column(table, sa.Column("tenant_id", sa.String(), nullable=True))
        op.create_foreign_key(f"fk_{table}_tenant_id", table, "tenants", ["tenant_id"], ["id"])
        op.execute(f"UPDATE {table} SET tenant_id = '{DEFAULT_TENANT}'")
        op.alter_column(table, "tenant_id", nullable=False, server_default=DEFAULT_TENANT)
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_constraint("fk_agents_tenant_id", "agents", type_="foreignkey")
    op.drop_column("agents", "tenant_id")
    for table in ("agent_executions", "conversations"):
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")
    op.drop_index("ix_credentials_tenant_id", table_name="credentials")
    op.drop_table("credentials")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
