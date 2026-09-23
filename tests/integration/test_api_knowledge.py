"""Knowledge routes (S8, ADR 0020 §6): dataset CRUD with config immutability,
delete-while-referenced 409, synchronous ingest (201 / 502-with-row / 422
cap), tenancy. The mock embedder rides the container's factory fallback
(`resolve_embedder` provider=="mock" → a fresh MockEmbeddingProvider,
dimension 8) — no DI needed."""

from __future__ import annotations

import pytest

from jarvis.domain.knowledge import EmbeddingConfig

EMBED = {"provider": "mock", "model": "mock-embedder", "dimension": 8}


def _dataset_payload(name: str = "notes") -> dict:
    return {
        "name": name,
        "description": "integration fixture",
        "embedding_config": EMBED,
        "chunk_config": {},
    }


async def _create_dataset(client, name: str = "notes") -> dict:
    resp = await client.post("/v1/knowledge/datasets", json=_dataset_payload(name))
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.db
async def test_dataset_crud_roundtrip(client):
    created = await _create_dataset(client)
    assert created["name"] == "notes"
    assert created["embedding_config"]["dimension"] == 8
    assert created["embedding_config"]["distance"] == "cosine"

    listed = (await client.get("/v1/knowledge/datasets")).json()
    assert [d["id"] for d in listed["items"]] == [created["id"]]

    detail = (await client.get(f"/v1/knowledge/datasets/{created['id']}")).json()
    assert detail == created

    patched = await client.patch(
        f"/v1/knowledge/datasets/{created['id']}",
        json={"name": "renamed", "description": "updated"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "renamed"
    assert patched.json()["embedding_config"] == created["embedding_config"]

    assert (await client.delete(f"/v1/knowledge/datasets/{created['id']}")).status_code == 204
    assert (await client.get(f"/v1/knowledge/datasets/{created['id']}")).status_code == 404


@pytest.mark.db
async def test_dataset_config_immutability_422(client):
    """A PATCH carrying any config field is a 422 at the schema boundary
    (D63) — the dimension is load-bearing for every stored vector."""
    created = await _create_dataset(client)
    resp = await client.patch(
        f"/v1/knowledge/datasets/{created['id']}",
        json={
            "name": "renamed",
            "description": "",
            "embedding_config": {"provider": "mock", "model": "other", "dimension": 16},
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.db
async def test_delete_referenced_dataset_409(client, container):
    """An agent version snapshot referencing the dataset pins it — delete
    refused with 409 and the reference count in the message (D63)."""
    created = await _create_dataset(client)
    resp = await client.post(
        "/v1/agents",
        json={
            "name": "kb-agent",
            "model": {"provider": "mock", "model": "mock-model"},
            "strategy": {"type": "function_calling"},
            "knowledge": {"datasets": [created["id"]], "top_k": 2},
        },
    )
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["definition"]["id"]  # the detail shape (CLAUDE.md note)
    # publish so a version snapshot contains the binding
    resp = await client.patch(f"/v1/agents/{agent_id}", json={"name": "kb-agent-v2"})
    assert resp.status_code == 200, resp.text

    delete = await client.delete(f"/v1/knowledge/datasets/{created['id']}")
    assert delete.status_code == 409, delete.text
    assert "referenced" in delete.json()["error"]["message"]

    # unbinding releases only FUTURE snapshots — versions 1 and 2 are
    # immutable and still reference the dataset (D63), so the delete stays 409
    resp = await client.patch(f"/v1/agents/{agent_id}", json={"knowledge": None})
    assert resp.status_code == 200, resp.text
    assert (await client.delete(f"/v1/knowledge/datasets/{created['id']}")).status_code == 409

    # removing the agent (and its snapshots) releases the dataset
    assert (await client.delete(f"/v1/agents/{agent_id}")).status_code == 204
    assert (await client.delete(f"/v1/knowledge/datasets/{created['id']}")).status_code == 204


@pytest.mark.db
async def test_document_ingest_happy_path(client):
    """Synchronous ingest (D65): 201 with status `ready` and the chunk
    count; the document listing carries no content beyond what the row
    reports."""
    # a small window forces the two paragraphs into two chunks
    resp = await client.post(
        "/v1/knowledge/datasets",
        json={
            "name": "notes",
            "embedding_config": EMBED,
            "chunk_config": {"chunk_chars": 100, "overlap_chars": 20},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    doc = await client.post(
        f"/v1/knowledge/datasets/{created['id']}/documents",
        json={"name": "doc-1", "content": "a" * 60 + "\n\n" + "b" * 60},
    )
    assert doc.status_code == 201, doc.text
    body = doc.json()
    assert body["status"] == "ready"
    assert body["error"] is None
    assert body["chunk_count"] == 2  # two paragraphs → two chunks

    listed = (await client.get(f"/v1/knowledge/datasets/{created['id']}/documents")).json()
    assert [d["id"] for d in listed["items"]] == [body["id"]]

    detail = (
        await client.get(f"/v1/knowledge/datasets/{created['id']}/documents/{body['id']}")
    ).json()
    assert detail["status"] == "ready"

    assert (
        await client.delete(f"/v1/knowledge/datasets/{created['id']}/documents/{body['id']}")
    ).status_code == 204
    listed = (await client.get(f"/v1/knowledge/datasets/{created['id']}/documents")).json()
    assert listed["items"] == []


@pytest.mark.db
async def test_document_ingest_provider_down_502_with_persisted_row(client):
    """An embedder failure is an honest receipt (D66/D65): the row is
    persisted with status `error` + the failure text, and the API answers
    502 embedding_unavailable — never a silent 201, never a 500."""
    created = await client.post(
        "/v1/knowledge/datasets",
        json={
            "name": "dead",
            "embedding_config": {
                "provider": "openai_compatible",
                "model": "qwen3-embedding:0.6b",
                "base_url": "http://127.0.0.1:9/v1",  # nothing listens here
                "dimension": 1024,
            },
            "chunk_config": {},
        },
    )
    assert created.status_code == 201, created.text
    dataset = created.json()

    doc = await client.post(
        f"/v1/knowledge/datasets/{dataset['id']}/documents",
        json={"name": "doomed", "content": "alpha paragraph\n\nbeta paragraph"},
    )
    assert doc.status_code == 502, doc.text
    assert doc.json()["error"]["kind"] == "embedding_unavailable"

    listed = (await client.get(f"/v1/knowledge/datasets/{dataset['id']}/documents")).json()
    assert len(listed["items"]) == 1
    row = listed["items"][0]
    assert row["name"] == "doomed"
    assert row["status"] == "error"
    assert row["error"]  # the receipt names the failure
    assert row["chunk_count"] == 0


@pytest.mark.db
async def test_document_ingest_content_cap_422(client, container):
    created = await _create_dataset(client)
    oversized = "x" * (container.settings.knowledge_max_doc_chars + 1)
    resp = await client.post(
        f"/v1/knowledge/datasets/{created['id']}/documents",
        json={"name": "too-big", "content": oversized},
    )
    assert resp.status_code == 422, resp.text
    assert "cap" in resp.json()["error"]["message"]

    # blank content is a 422 too (schema min_length covers "", whitespace is
    # caught by the route's strip check)
    resp = await client.post(
        f"/v1/knowledge/datasets/{created['id']}/documents",
        json={"name": "blank", "content": "   "},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.db
async def test_tenant_scoping_404(container, client):
    await container.auth.create_tenant("t-kb-other", "Other")
    foreign = await container.knowledge.create_dataset(
        __import__("jarvis.domain.knowledge", fromlist=["KnowledgeDataset"]).KnowledgeDataset(
            id="ds-foreign",
            name="foreign",
            embedding_config=EmbeddingConfig(**EMBED),
            chunk_config=__import__(
                "jarvis.domain.knowledge", fromlist=["ChunkConfig"]
            ).ChunkConfig(),
        ),
        tenant_id="t-kb-other",
    )
    assert (await client.get(f"/v1/knowledge/datasets/{foreign.id}")).status_code == 404
    listed = (await client.get("/v1/knowledge/datasets")).json()
    assert foreign.id not in [d["id"] for d in listed["items"]]
    assert (
        await client.post(
            f"/v1/knowledge/datasets/{foreign.id}/documents",
            json={"name": "sneaky", "content": "paragraph"},
        )
    ).status_code == 404
    assert (await client.delete(f"/v1/knowledge/datasets/{foreign.id}")).status_code == 404
