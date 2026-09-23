"""The D68 one-call retrieval contract (S8, ADR 0020 §6).

`KnowledgeRetrieval.retrieve(datasets, query, ...)` → the rendered
context string. Hybrid search / rerank EXTEND THIS SERVICE ONLY — the
runtime, the route, and the tool provider all consume the same call.
Tenancy stays at the edges: callers pass tenant-VALIDATED datasets
(the route and the runtime resolve them scoped first); the service owns
the embedding-model invariant, query embedding, search, min_score
filtering and rendering. Embedder failures propagate — the caller maps
them (the route → 502 embedding_unavailable; the runtime → terminal
`tool` failure)."""

from __future__ import annotations

from jarvis.domain.auth import Principal
from jarvis.domain.knowledge import EmbeddingConfig, KnowledgeChunk, KnowledgeDataset
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.types import EmbeddingRequest
from jarvis.ports.repository import KnowledgeRepo


class RetrievalError(Exception):
    """A retrieval precondition failed (embedding-model mismatch)."""


def embedder_signature(config: EmbeddingConfig) -> tuple[str | None, ...]:
    """Identity of an embedding setup: provider + model + base_url +
    dimension. Credential refs are deliberately EXCLUDED — the same
    model reachable through different credentials is still the same
    embedding space. Public: the agents route's save-time 422 checks
    the SAME signature (ADR 0020 §4 — one query embedding serves the
    whole binding)."""
    return (config.provider, config.model, config.base_url, str(config.dimension))


class KnowledgeRetrieval:
    """One service, one call (D68). Built once per app container."""

    def __init__(
        self,
        *,
        repo: KnowledgeRepo,
        models: DefaultModelProviderFactory,
        default_top_k: int = 4,
    ) -> None:
        self._repo = repo
        self._models = models
        self._default_top_k = default_top_k

    async def retrieve(
        self,
        datasets: list[KnowledgeDataset],
        query: str,
        *,
        tenant_id: str,
        principal: Principal | None = None,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> str:
        """Embed the query, search across the datasets, render the top
        hits as `[doc name] content` blocks. No hits → empty string."""
        if not datasets:
            return ""
        self._check_one_embedding_model(datasets)
        embedding_config = datasets[0].embedding_config
        embedder = await self._models.resolve_embedder(embedding_config, principal=principal)
        response = await embedder.embed(
            EmbeddingRequest(model=embedding_config.model, input=[query])
        )
        hits = await self._repo.search_chunks(
            [dataset.id for dataset in datasets],
            response.vectors[0],
            limit=top_k or self._default_top_k,
        )
        selected = [
            hit
            for hit in hits
            if min_score is None or (hit.score is not None and hit.score >= min_score)
        ]
        return _render(selected)

    @staticmethod
    def _check_one_embedding_model(datasets: list[KnowledgeDataset]) -> None:
        """The one-embedding-model invariant (ADR 0020 §4, D66): a mixed
        binding cannot search — the vectors live in different spaces.
        Definition save refuses the mix (422); this is defense-in-depth."""
        signatures = {embedder_signature(dataset.embedding_config) for dataset in datasets}
        if len(signatures) > 1:
            raise RetrievalError(
                "a knowledge binding may only reference datasets sharing one "
                "embedding model (provider/model/base_url/dimension) — mixed "
                f"binding across {len(signatures)} different configs"
            )


def _render(hits: list[KnowledgeChunk]) -> str:
    """`[doc name] content` blocks, blank-line separated (ADR 0020 §6)."""
    return "\n\n".join(f"[{hit.document_name}] {hit.content}" for hit in hits)


__all__ = ["KnowledgeRetrieval", "RetrievalError", "embedder_signature"]
