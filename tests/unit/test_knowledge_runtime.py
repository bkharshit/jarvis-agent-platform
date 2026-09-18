"""Knowledge runtime (S8): the pure chunker's packing/overlap/hard-split
behavior and the D68 retrieval service — one call, one-embedding-model
invariant, min_score filter, the `[doc name] content` render."""

from __future__ import annotations

import pytest

from jarvis.domain.auth import Principal
from jarvis.domain.knowledge import ChunkConfig, EmbeddingConfig, KnowledgeChunk, KnowledgeDataset
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockEmbeddingProvider
from jarvis.runtime.knowledge.chunking import chunk_text
from jarvis.runtime.knowledge.retrieval import KnowledgeRetrieval, RetrievalError

_PRINCIPAL = Principal(tenant_id="t1", user_id="u1", mode="session")


def _dataset(name: str, *, dimension: int = 8, model: str = "mock-embedder") -> KnowledgeDataset:
    return KnowledgeDataset(
        id=f"ds-{name}",
        name=name,
        embedding_config=EmbeddingConfig(provider="mock", model=model, dimension=dimension),
    )


class TestChunkText:
    def test_short_text_is_one_chunk_unchanged(self):
        text = "Just a paragraph."
        assert chunk_text(text, ChunkConfig(chunk_chars=100, overlap_chars=10)) == [text]

    def test_empty_text_yields_nothing(self):
        assert chunk_text("   \n\n  ", ChunkConfig()) == []

    def test_paragraphs_pack_respecting_boundaries(self):
        paragraphs = [f"P{i} " + "x" * 30 for i in range(6)]
        chunks = chunk_text("\n\n".join(paragraphs), ChunkConfig(chunk_chars=100, overlap_chars=0))
        # Every chunk ≤ 100 chars; every paragraph appears intact somewhere.
        assert all(len(chunk) <= 100 for chunk in chunks)
        for paragraph in paragraphs:
            assert any(paragraph in chunk for chunk in chunks)

    def test_consecutive_chunks_carry_overlap(self):
        words = " ".join(f"w{i}" for i in range(40))
        chunks = chunk_text(words, ChunkConfig(chunk_chars=100, overlap_chars=12))
        assert len(chunks) >= 2
        # The property, not a fixture string: chunk N+1 OPENS with a tail
        # of chunk N (the overlap carry, cut at a word boundary).
        carry = chunks[1].split("\n\n")[0]
        assert carry and carry in chunks[0][-16:]

    def test_oversized_paragraph_is_hard_split(self):
        text = "y" * 250
        chunks = chunk_text(text, ChunkConfig(chunk_chars=100, overlap_chars=0))
        assert all(len(chunk) <= 100 for chunk in chunks)
        # No overlap carry → the pieces tile the paragraph exactly.
        assert "".join(chunks).count("y") == 250

    def test_deterministic(self):
        text = "alpha\n\nbeta\n\ngamma " * 20
        assert chunk_text(text, ChunkConfig()) == chunk_text(text, ChunkConfig())


class TestKnowledgeRetrieval:
    def _service(self, hits: list[KnowledgeChunk], provider: MockEmbeddingProvider):
        class FakeRepo:
            def __init__(self) -> None:
                self.searched: list[tuple[list[str], list[float], int]] = []

            async def search_chunks(
                self, dataset_ids: list[str], embedding: list[float], limit: int
            ) -> list[KnowledgeChunk]:
                self.searched.append((dataset_ids, embedding, limit))
                return hits

        repo = FakeRepo()
        models = DefaultModelProviderFactory(mock_embedding_provider=provider)
        service = KnowledgeRetrieval(repo=repo, models=models, default_top_k=4)  # type: ignore[arg-type]
        return service, repo

    async def test_renders_doc_name_blocks(self):
        hits = [
            KnowledgeChunk(
                id="c1",
                document_id="d1",
                dataset_id="ds-a",
                chunk_index=0,
                content="first chunk body",
                embedding=[1.0],
                document_name="refund-policy",
                score=0.9,
            ),
            KnowledgeChunk(
                id="c2",
                document_id="d1",
                dataset_id="ds-a",
                chunk_index=1,
                content="second chunk body",
                embedding=[0.5],
                document_name="refund-policy",
                score=0.8,
            ),
        ]
        service, repo = self._service(hits, MockEmbeddingProvider(dimension=8))
        context = await service.retrieve([_dataset("a")], "how do refunds work", tenant_id="t1")
        assert context == "[refund-policy] first chunk body\n\n[refund-policy] second chunk body"
        assert repo.searched[0][0] == ["ds-a"]
        assert repo.searched[0][2] == 4  # default_top_k

    async def test_no_hits_render_empty(self):
        service, _ = self._service([], MockEmbeddingProvider(dimension=8))
        context = await service.retrieve([_dataset("a")], "anything", tenant_id="t1")
        assert context == ""

    async def test_min_score_filters_by_search_score(self):
        hits = [
            KnowledgeChunk(
                id="c1",
                document_id="d1",
                dataset_id="ds-a",
                chunk_index=0,
                content="strong",
                embedding=[1.0],
                document_name="doc",
                score=0.9,
            ),
            KnowledgeChunk(
                id="c2",
                document_id="d1",
                dataset_id="ds-a",
                chunk_index=1,
                content="weak",
                embedding=[0.2],
                document_name="doc",
                score=0.3,
            ),
        ]
        service, _ = self._service(hits, MockEmbeddingProvider(dimension=8))
        context = await service.retrieve([_dataset("a")], "query", tenant_id="t1", min_score=0.5)
        assert context == "[doc] strong"

    async def test_binding_top_k_wins_over_default(self):
        service, repo = self._service([], MockEmbeddingProvider(dimension=8))
        await service.retrieve([_dataset("a")], "q", tenant_id="t1", top_k=7)
        assert repo.searched[0][2] == 7

    async def test_mixed_embedding_configs_refused(self):
        datasets = [_dataset("a"), _dataset("b", model="other-embedder")]
        service, _ = self._service([], MockEmbeddingProvider(dimension=8))
        with pytest.raises(RetrievalError):
            await service.retrieve(datasets, "q", tenant_id="t1")

    async def test_empty_dataset_list_is_empty_context(self):
        service, _ = self._service([], MockEmbeddingProvider(dimension=8))
        assert await service.retrieve([], "q", tenant_id="t1") == ""

    async def test_embedder_failure_propagates_to_the_caller(self):
        failure = ConnectionError("embedding backend down")
        service, _ = self._service([], MockEmbeddingProvider(dimension=8, errors=[failure]))
        with pytest.raises(ConnectionError):
            await service.retrieve([_dataset("a")], "q", tenant_id="t1", principal=_PRINCIPAL)
