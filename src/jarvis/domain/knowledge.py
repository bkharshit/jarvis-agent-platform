"""Knowledge datasets (S8, ADR 0020 §1, D63) — pure Pydantic, no IO.

The embedding config is IMMUTABLE once a dataset exists: the dimension is
pinned at create and every vector written to the dataset must match it
("change the embedding model" = create a new dataset). Documents and
chunks carry no tenant — every read joins through the tenant-scoped
dataset (the eval_results precedent, D29). Embeddings end at
`list[float]` here — pgvector lives only in the persistence layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.domain.agent import CredentialRef

KnowledgeDocumentStatus = Literal["ready", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingConfig(_Model):
    """The dataset's pinned embedding model — provider/model/base_url/
    credential_ref/dimension/distance all persisted WITH the index. A
    PATCH that changes any of it is refused (the wholesale-PATCH lesson:
    round-trip byte-identical or 422)."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str | None = None
    credential_ref: CredentialRef | None = None
    dimension: int = Field(gt=0)
    distance: Literal["cosine"] = "cosine"


class ChunkConfig(_Model):
    """Paragraph-aware packing parameters (D65): chunks pack to
    `chunk_chars` with an `overlap_chars` tail carry."""

    chunk_chars: int = Field(default=1_200, ge=100, le=8_000)
    overlap_chars: int = Field(default=150, ge=0, le=1_000)

    @model_validator(mode="after")
    def _overlap_fits(self) -> ChunkConfig:
        if self.overlap_chars >= self.chunk_chars:
            raise ValueError("overlap_chars must be smaller than chunk_chars")
        return self


class KnowledgeDataset(_Model):
    id: str
    name: str = Field(min_length=1)
    description: str = ""
    embedding_config: EmbeddingConfig
    chunk_config: ChunkConfig = Field(default_factory=ChunkConfig)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset name must not be blank")
        return value


class KnowledgeDocument(_Model):
    """One text document. `content` is the re-chunk/re-embed source of
    truth (kept verbatim); `status='error'` carries the ingest failure
    honestly (the document row persists, the route answers 502)."""

    id: str
    dataset_id: str
    name: str = Field(min_length=1)
    content: str = Field(min_length=1)
    status: KnowledgeDocumentStatus = "ready"
    error: str | None = None
    chunk_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class KnowledgeChunk(_Model):
    """One chunk: its embedding is the pinned dimension's vector (validated
    against the dataset's config at WRITE time). `document_name` and
    `score` are SEARCH-ONLY projections — the retrieval query joins the
    document's name and computes the similarity; a persisted chunk row
    leaves them None."""

    id: str
    document_id: str
    dataset_id: str
    chunk_index: int = Field(ge=0)
    content: str
    embedding: list[float]
    document_name: str | None = None
    score: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "ChunkConfig",
    "EmbeddingConfig",
    "KnowledgeChunk",
    "KnowledgeDataset",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
]
