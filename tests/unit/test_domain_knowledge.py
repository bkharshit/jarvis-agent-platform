"""S8 (ADR 0020 §1, D63): knowledge domain types — dimension pinning,
chunk-config rules, immutability posture."""

import pytest
from pydantic import ValidationError

from jarvis.domain.knowledge import (
    ChunkConfig,
    EmbeddingConfig,
    KnowledgeChunk,
    KnowledgeDataset,
    KnowledgeDocument,
)


def _embedding_config(**overrides):
    base = dict(provider="openai_compatible", model="qwen3-embedding:0.6b", dimension=1024)
    base.update(overrides)
    return EmbeddingConfig(**base)


class TestEmbeddingConfig:
    def test_dimension_must_be_positive(self):
        with pytest.raises(ValidationError):
            _embedding_config(dimension=0)
        with pytest.raises(ValidationError):
            _embedding_config(dimension=-8)

    def test_distance_is_cosine_only(self):
        assert _embedding_config().distance == "cosine"
        with pytest.raises(ValidationError):
            _embedding_config(distance="l2")

    def test_credential_ref_rides_the_union(self):
        config = _embedding_config(credential_ref={"type": "env", "env_var": "OLLAMA_API_KEY"})
        assert config.credential_ref is not None
        assert config.credential_ref.type == "env"

    def test_unknown_fields_forbid(self):
        with pytest.raises(ValidationError):
            _embedding_config(api_key="sk-never")  # secrets never enter domain


class TestChunkConfig:
    def test_defaults(self):
        config = ChunkConfig()
        assert config.chunk_chars == 1_200
        assert config.overlap_chars == 150

    def test_overlap_must_fit_inside_chunk(self):
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_chars=100, overlap_chars=100)
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_chars=200, overlap_chars=500)

    def test_bounds(self):
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_chars=50)
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_chars=10_000)


class TestKnowledgeDataset:
    def test_minimal_dataset(self):
        dataset = KnowledgeDataset(id="ds-1", name="docs", embedding_config=_embedding_config())
        assert dataset.chunk_config == ChunkConfig()
        assert dataset.description == ""

    def test_dataset_requires_name(self):
        with pytest.raises(ValidationError):
            KnowledgeDataset(id="ds-1", name=" ", embedding_config=_embedding_config())


class TestKnowledgeDocument:
    def test_content_required(self):
        document = KnowledgeDocument(id="doc-1", dataset_id="ds-1", name="a", content="hello")
        assert document.status == "ready"
        assert document.chunk_count == 0
        with pytest.raises(ValidationError):
            KnowledgeDocument(id="doc-2", dataset_id="ds-1", name="b", content="")


class TestKnowledgeChunk:
    def test_chunk_holds_a_plain_vector(self):
        chunk = KnowledgeChunk(
            id="c-1",
            document_id="doc-1",
            dataset_id="ds-1",
            chunk_index=0,
            content="text",
            embedding=[0.1, 0.2],
        )
        assert chunk.embedding == [0.1, 0.2]
        with pytest.raises(ValidationError):
            KnowledgeChunk(
                id="c-2",
                document_id="doc-1",
                dataset_id="ds-1",
                chunk_index=-1,
                content="t",
                embedding=[0.1],
            )
