"""Embeddings (S8, ADR 0020 §2, D64): the OpenAI-compatible adapter's
`/embeddings` translation and error mapping, the MockEmbeddingProvider's
deterministic-meaningful vectors, and the factory's `resolve_embedder`
(the D28 pattern). httpx is mocked with respx — zero network."""

from __future__ import annotations

import math

import httpx
import pytest
import respx

from jarvis.domain.agent import EnvCredentialRef, StoredCredentialRef
from jarvis.domain.auth import Principal
from jarvis.domain.knowledge import EmbeddingConfig
from jarvis.models.credentials import DefaultCredentialResolver
from jarvis.models.errors import (
    ModelAuthError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelStreamError,
)
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockEmbeddingProvider, mock_embedding
from jarvis.models.openai_compatible import OpenAICompatibleProvider
from jarvis.models.types import EmbeddingRequest

BASE = "http://localhost:9999/v1"
URL = f"{BASE}/embeddings"

_PRINCIPAL = Principal(tenant_id="t1", user_id="u1", mode="session")


def _provider(**kw) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(base_url=BASE, **kw)


def _request(**kw) -> EmbeddingRequest:
    defaults = dict(model="emb-1", input=["hello world"])
    defaults.update(kw)
    return EmbeddingRequest(**defaults)


def _config(provider: str = "openai_compatible", **kw) -> EmbeddingConfig:
    defaults = dict(provider=provider, model="emb-1", base_url=BASE, dimension=8)
    defaults.update(kw)
    return EmbeddingConfig(**defaults)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class TestOpenAICompatibleEmbed:
    @respx.mock
    async def test_embed_translates_request_and_response(self):
        route = respx.post(URL).respond(
            json={
                "data": [
                    {"index": 0, "embedding": [1.0, 2.0]},
                    {"index": 1, "embedding": [3.0, 4.0]},
                ],
                "usage": {"prompt_tokens": 7},
            }
        )
        provider = _provider()
        result = await provider.embed(_request(input=["a", "b"]))
        assert route.called
        assert result.vectors == [[1.0, 2.0], [3.0, 4.0]]
        assert result.usage is not None
        assert result.usage.input_tokens == 7

    @respx.mock
    async def test_embed_orders_vectors_by_index_not_wire_order(self):
        respx.post(URL).respond(
            json={
                "data": [
                    {"index": 1, "embedding": [3.0]},
                    {"index": 0, "embedding": [1.0]},
                ]
            }
        )
        result = await _provider().embed(_request(input=["a", "b"]))
        assert result.vectors == [[1.0], [3.0]]

    @respx.mock
    async def test_embed_rejects_vector_count_mismatch(self):
        respx.post(URL).respond(json={"data": [{"index": 0, "embedding": [1.0]}]})
        with pytest.raises(ModelStreamError):
            await _provider().embed(_request(input=["a", "b"]))

    @respx.mock
    async def test_embed_maps_http_errors_like_chat(self):
        respx.post(URL).respond(status_code=401, text="bad key")
        with pytest.raises(ModelAuthError):
            await _provider().embed(_request())
        respx.post(URL).respond(status_code=429, text="slow down")
        with pytest.raises(ModelRateLimitError):
            await _provider().embed(_request())
        respx.post(URL).respond(status_code=500, text="boom")
        with pytest.raises(ModelConnectionError):
            await _provider().embed(_request())

    @respx.mock
    async def test_embed_maps_connection_failure(self):
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ModelConnectionError):
            await _provider().embed(_request())


class TestMockEmbeddingProvider:
    async def test_deterministic_and_unit_length(self):
        provider = MockEmbeddingProvider(dimension=8)
        first = await provider.embed(_request(input=["billing refund"]))
        second = await MockEmbeddingProvider(dimension=8).embed(_request(input=["billing refund"]))
        assert first.vectors[0] == second.vectors[0]
        assert math.isclose(sum(v * v for v in first.vectors[0]), 1.0)

    async def test_cosine_similarity_is_meaningful(self):
        # Shared tokens → shared buckets: the "billing" chunk ranks above
        # the "cooking" chunk for the query. This is what makes retrieval
        # ranking REAL in tests.
        query = mock_embedding("billing refund policy", 8)
        billing = mock_embedding("full billing refund policy text", 8)
        cooking = mock_embedding("best cooking recipes pasta", 8)
        assert _cosine(query, billing) > _cosine(query, cooking)
        assert _cosine(query, billing) < 0.999  # close, not identical

    async def test_records_requests_and_injects_errors(self):
        failure = ModelConnectionError("embedding backend down", provider="mock")
        provider = MockEmbeddingProvider(errors=[failure])
        with pytest.raises(ModelConnectionError):
            await provider.embed(_request())
        assert len(provider.requests) == 1  # recorded before the raise
        # The scripted error is consumed; a second call succeeds.
        result = await provider.embed(_request(input=["ok"]))
        assert result.vectors == [mock_embedding("ok", 8)]


class TestFactoryResolveEmbedder:
    async def test_mock_config_returns_the_shared_mock(self):
        shared = MockEmbeddingProvider(dimension=4)
        factory = DefaultModelProviderFactory(mock_embedding_provider=shared)
        embedder = await factory.resolve_embedder(_config(provider="mock"))
        assert embedder is shared

    async def test_mock_config_without_injection_builds_one(self):
        factory = DefaultModelProviderFactory()
        embedder = await factory.resolve_embedder(_config(provider="mock"))
        assert isinstance(embedder, MockEmbeddingProvider)

    @respx.mock
    async def test_env_ref_rides_the_openai_adapter(self):
        respx.post(URL).respond(json={"data": [{"index": 0, "embedding": [1.0, 0.5]}]})
        factory = DefaultModelProviderFactory(api_key_override="sk-test")
        embedder = await factory.resolve_embedder(
            _config(credential_ref=EnvCredentialRef(type="env", env_var="OPENAI_API_KEY"))
        )
        result = await embedder.embed(_request())
        assert result.vectors == [[1.0, 0.5]]
        assert respx.calls.last.request.headers["Authorization"] == "Bearer sk-test"

    async def test_stored_credential_requires_a_principal(self):
        factory = DefaultModelProviderFactory()
        with pytest.raises(ModelAuthError):
            await factory.resolve_embedder(
                _config(credential_ref=StoredCredentialRef(type="stored", credential_id="c1"))
            )

    async def test_stored_credential_materializes_tenant_scoped(self):
        seen: dict[str, object] = {}

        class Stored:
            async def resolve(self, principal, ref):
                seen["principal"] = principal
                from jarvis.ports.credential import ResolvedMaterial

                return ResolvedMaterial(type="material", value="sk-live")

        factory = DefaultModelProviderFactory(
            credential_resolver=DefaultCredentialResolver(stored_resolver=Stored())
        )
        embedder = await factory.resolve_embedder(
            _config(credential_ref=StoredCredentialRef(type="stored", credential_id="c1")),
            principal=_PRINCIPAL,
        )
        assert seen["principal"] == _PRINCIPAL
        assert embedder is not None  # the material lives in the instance
