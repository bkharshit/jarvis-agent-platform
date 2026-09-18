"""Knowledge / RAG tables (S8, ADR 0020 §1, D63)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-18

`knowledge_datasets` / `knowledge_documents` / `knowledge_chunks` — the
pgvector-backed store. The embedding config is IMMUTABLE on the dataset
(dimensions pinned at create; "change the model" = new dataset); documents
and chunks carry no tenant — every read joins through the tenant-scoped
dataset (the eval_results precedent). `CREATE EXTENSION IF NOT EXISTS
vector` comes FIRST; the downgrade NEVER drops the extension (it may be
shared).
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import UserDefinedType

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class _Vector(UserDefinedType[list[float]]):
    """pgvector's `vector` type, dimension-free (the dimension is enforced
    at WRITE time from the dataset's pinned config, never by the column).
    Self-contained here on purpose — migrations must not drift with app
    code; the runtime twin lives in jarvis.persistence.types."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "VECTOR"


def upgrade() -> None:
    # The extension FIRST — nothing below works without it. IF NOT EXISTS
    # keeps re-runs (and datasets where another migration created it) safe.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_datasets",
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
        sa.Column("embedding_config", JSONB(), nullable=False),
        sa.Column("chunk_config", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_knowledge_datasets_tenant_name"),
    )
    op.create_index("ix_knowledge_datasets_tenant_id", "knowledge_datasets", ["tenant_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "dataset_id",
            sa.String(),
            sa.ForeignKey("knowledge_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Enum("ready", "error", name="knowledge_document_status"), nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_documents_dataset_id", "knowledge_documents", ["dataset_id"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized for scoped search — chunk queries always WHERE
        # dataset_id IN (tenant-visible datasets).
        sa.Column("dataset_id", sa.String(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", _Vector(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index("ix_knowledge_chunks_dataset_id", "knowledge_chunks", ["dataset_id"])
    # NO HNSW index here (the ADR 0020 §1 sketch had one): pgvector refuses
    # to index a dimension-free `vector` column ("column does not have
    # dimensions") and pinning the index to one dimension would reject every
    # dataset pinned to another. Search is a sequential scan with `<=>` —
    # correct at platform scale; per-dataset indexes (or a fixed platform
    # dimension) are the future path.


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_dataset_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_dataset_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_knowledge_datasets_tenant_id", table_name="knowledge_datasets")
    op.drop_table("knowledge_datasets")
    # drop_table leaves the enum behind (the 0003/0013 precedent) — without
    # this a downgrade/upgrade cycle dies on CREATE TYPE (the D4 gotcha).
    op.execute("DROP TYPE IF EXISTS knowledge_document_status")
    # NOTE: the vector extension is deliberately NOT dropped — it may be
    # shared by other databases on this server (ADR 0020 §1).
