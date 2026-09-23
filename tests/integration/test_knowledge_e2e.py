"""S8 (ADR 0020) end-to-end honesty paths over HTTP + the live worker: the
D66 injection visible in the ACTUAL model request (llm-trace receipts), a
`retriever__` tool call riding the full function_calling loop, the
deleted/foreign dataset terminal failures (D29: a foreign id reads as
absent), and the dimension-mismatch refusal at run time.

Delete-while-referenced 409 and route-level tenant scoping live in
test_api_knowledge.py (commit 7); repo-level dimension refusal lives in
test_knowledge_repo.py. This file is the run-path honesty layer."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jarvis.domain.agent import AgentDefinition, KnowledgeBinding, ModelRef, StrategyConfig
from jarvis.domain.knowledge import EmbeddingConfig, KnowledgeDataset
from jarvis.domain.message import ToolCall
from jarvis.models.mock import turn

pytestmark = pytest.mark.db

CREATE = {
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}

EMBED = {"provider": "mock", "model": "mock-embedder", "dimension": 8}


async def _make(client, name: str, **overrides) -> dict:
    """Create + publish an agent; the create response IS the AgentDetail."""
    resp = await client.post("/v1/agents", json={"name": name, **CREATE, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _dataset(client, name: str = "notes", **overrides) -> dict:
    resp = await client.post(
        "/v1/knowledge/datasets",
        json={"name": name, "embedding_config": EMBED, "chunk_config": {}, **overrides},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _ingest(client, dataset_id: str, name: str, content: str) -> dict:
    resp = await client.post(
        f"/v1/knowledge/datasets/{dataset_id}/documents",
        json={"name": name, "content": content},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _await_terminal(client, run_id: str) -> dict:
    for _ in range(200):
        resp = await client.get(f"/v1/executions/{run_id}")
        assert resp.status_code == 200, resp.text
        row = resp.json()["run"]
        if row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal state")


# --- the injection receipt (D66) -------------------------------------------------


@pytest.mark.db
async def test_run_injects_knowledge_visible_in_llm_trace(client, container, mock):
    """The full stack: real dataset + document over pgvector, a published
    agent with a knowledge binding, a queue run — and the ACTUAL model
    request carries the `Relevant knowledge base excerpts` system message
    (the llm-trace receipt, ADR 0014)."""
    container.settings.llm_trace = True
    container.runtime._trace_llm = True
    dataset = await _dataset(client, "capitals")
    await _ingest(client, dataset["id"], "capitals.md", "the capital of France is Paris")
    mock.add_turn(turn("answered from knowledge"))
    agent = await _make(client, "kb-agent", knowledge={"datasets": [dataset["id"]], "top_k": 2})
    run_id = (
        await client.post(f"/v1/agents/{agent['definition']['id']}/run", json={"input": "hi"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "succeeded"

    trace = (await client.get(f"/v1/executions/{run_id}/llm-trace")).json()
    assert len(trace["entries"]) == 1
    messages = trace["entries"][0]["request"]["messages"]
    system = [m for m in messages if m["role"] == "system"]
    assert len(system) == 1
    assert system[0]["content"] == (
        "Relevant knowledge base excerpts:\n\n[capitals.md] the capital of France is Paris"
    )


# --- the retriever tool family over the real loop --------------------------------


@pytest.mark.db
async def test_retriever_tool_call_in_function_calling_run(client, mock):
    """The model calls `retriever__<dataset>` through the real ToolRuntime;
    the tool's rendered context comes back as the tool result and the final
    answer reflects it."""
    dataset = await _dataset(client, "facts")
    await _ingest(client, dataset["id"], "facts.md", "the Eiffel Tower is in Paris")
    tool_name = "retriever__facts"
    agent = await _make(
        client,
        "retriever-agent",
        tools=[{"name": tool_name, "config": {"dataset_id": dataset["id"], "top_k": 3}}],
    )
    agent_id = agent["definition"]["id"]
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name=tool_name, arguments={"query": "eiffel"})])
    )
    mock.add_turn(turn("The Eiffel Tower is in Paris."))
    run_id = (
        await client.post(f"/v1/agents/{agent_id}/run", json={"input": "where is the tower?"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "succeeded"

    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    tools = detail["tool_executions"]
    assert len(tools) == 1
    # ToolResult.output is TOP-LEVEL in API JSON (the D17 note)
    assert tools[0]["output"] == "[facts.md] the Eiffel Tower is in Paris"
    assert detail["messages"][-1]["content"] == "The Eiffel Tower is in Paris."


@pytest.mark.db
async def test_retriever_miss_is_an_honest_empty_answer(client, mock):
    """No hits → the tool answers `no relevant knowledge found`, the run
    still SUCCEEDS — a miss is data, never an error."""
    dataset = await _dataset(client, "empty")
    tool_name = "retriever__empty"
    agent = await _make(
        client, "miss-agent", tools=[{"name": tool_name, "config": {"dataset_id": dataset["id"]}}]
    )
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name=tool_name, arguments={"query": "anything"})])
    )
    mock.add_turn(turn("I don't know."))
    run_id = (
        await client.post(f"/v1/agents/{agent['definition']['id']}/run", json={"input": "?"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "succeeded"
    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert detail["tool_executions"][0]["output"] == "no relevant knowledge found for this query"


# --- honesty: the dataset vanished (D29/D66) -------------------------------------


@pytest.mark.db
async def test_deleted_dataset_is_a_terminal_tool_failure(container, client, mock):
    """A published snapshot can outlive its dataset only through the window
    the 409 guard cannot close (raw deletion); the run then fails honestly
    at retrieval-resolution — terminal `tool`, the dataset named (D66)."""
    dataset = await _dataset(client, "doomed")
    agent = await _make(client, "kb-doomed", knowledge={"datasets": [dataset["id"]], "top_k": 2})
    agent_id = agent["definition"]["id"]
    # bypass the delete-while-referenced guard: the honesty path the guard
    # narrows, not one it can eliminate (a foreign tenant's row, a race)
    sessionmaker = async_sessionmaker(container.engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM knowledge_datasets WHERE id = :id"), {"id": dataset["id"]}
        )
        await session.commit()
    mock.add_turn(turn("never reached"))
    run_id = (await client.post(f"/v1/agents/{agent_id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "failed"
    assert row["error_kind"] == "tool"
    assert "dataset not found in this tenant" in row["error"]


@pytest.mark.db
async def test_foreign_dataset_binding_fails_the_run_not_the_tenant(container, client, mock):
    """D29 transposed to the runtime: a snapshot pinned BEFORE the save-time
    422 existed (or a raw publish) with a foreign dataset — the run is a
    terminal `tool` failure naming the retriever, and NO other tenant's data
    is ever touched."""
    await container.auth.create_tenant("t-e2e-other", "Other")
    other = await container.knowledge.create_dataset(
        KnowledgeDataset(
            id="ds-e2e-foreign",
            name="foreign",
            embedding_config=EmbeddingConfig(**EMBED),
        ),
        tenant_id="t-e2e-other",
    )
    # publish through the repo directly — the save-time 422 correctly refuses
    # this binding over HTTP
    definition = AgentDefinition(
        id=str(uuid4()),
        name="foreign-binding",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        knowledge=KnowledgeBinding(datasets=[other.id]),
    )
    await container.agents.create(definition)
    await container.agents.update_and_publish(definition)
    agent_id = definition.id
    mock.add_turn(turn("never reached"))
    run_id = (await client.post(f"/v1/agents/{agent_id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "failed"
    assert row["error_kind"] == "tool"
    assert f"retriever__{other.id}" in row["error"]
    assert "dataset not found in this tenant" in row["error"]


# --- honesty: the embedder / dimension space -------------------------------------


@pytest.mark.db
async def test_dimension_mismatch_refuses_the_run(container, client, mock):
    """A dataset pinned to a dimension its embedder does not produce (a raw
    row the ingest route would never create) — the query embedding lands
    against a different vector space and the run fails TERMINALLY (the
    never-retry guarantee, D5), naming the mismatch."""
    dataset = await container.knowledge.create_dataset(
        KnowledgeDataset(
            id="ds-wide",
            name="wide",
            embedding_config=EmbeddingConfig(provider="mock", model="m", dimension=16),
        ),
        tenant_id="default",
    )
    # the ingest route would refuse 16-dim vectors from the 8-dim mock
    # embedder — plant them directly, the way a mis-pinned dataset happens
    from jarvis.domain.knowledge import KnowledgeChunk, KnowledgeDocument

    doc = KnowledgeDocument(
        id="doc-wide", dataset_id=dataset.id, name="wide.md", content="wide fact", status="ready"
    )
    await container.knowledge.create_document(
        doc,
        [
            KnowledgeChunk(
                id="ch-wide",
                document_id=doc.id,
                dataset_id=dataset.id,
                chunk_index=0,
                content="wide fact",
                embedding=[0.1] * 16,
            )
        ],
        tenant_id="default",
    )
    agent = await _make(client, "kb-wide", knowledge={"datasets": [dataset.id], "top_k": 2})
    mock.add_turn(turn("never reached"))
    run_id = (
        await client.post(f"/v1/agents/{agent['definition']['id']}/run", json={"input": "hi"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "failed"
    assert "internal error" in row["error"]
    assert "different vector dimensions" in row["error"]

    # the failed run's error carries the DBAPIError shape — assert it is the
    # pgvector dimension refusal, not a generic crash
    with pytest.raises(DBAPIError):
        await container.knowledge.search_chunks([dataset.id], [1.0] * 8, limit=1)


@pytest.mark.db
async def test_embedding_provider_down_is_a_terminal_model_failure(client, container):
    """A bound dataset whose embedder is unreachable (dead base_url, the
    D28 discipline) — the run fails honestly; the failure message names the
    transport error, never a silent empty context."""
    dead = await _dataset(
        client,
        "dead-embedder",
        embedding_config={
            "provider": "openai_compatible",
            "model": "qwen3-embedding:0.6b",
            "base_url": "http://127.0.0.1:9/v1",
            "dimension": 8,
        },
    )
    agent = await _make(client, "kb-dead", knowledge={"datasets": [dead["id"]], "top_k": 2})
    run_id = (
        await client.post(f"/v1/agents/{agent['definition']['id']}/run", json={"input": "hi"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "failed"
    # a connect failure rides the model-error catch (the same class as a
    # model credential failure) — honest either way (the D28 note)
    assert row["error_kind"] in ("model", "tool")


# --- mixed binding 422 over HTTP (the save-time gate) ----------------------------


@pytest.mark.db
async def test_mixed_embedding_binding_is_a_422_over_http(client, container):
    other = await container.knowledge.create_dataset(
        KnowledgeDataset(
            id="ds-mixed",
            name="mixed",
            embedding_config=EmbeddingConfig(
                provider="openai_compatible",
                model="other",
                dimension=8,
            ),
        ),
        tenant_id="default",
    )
    same_space = await _dataset(client, "same-space")
    resp = await client.post(
        "/v1/agents",
        json={
            "name": "mixed-agent",
            **CREATE,
            "knowledge": {"datasets": [same_space["id"], other.id], "top_k": 2},
        },
    )
    assert resp.status_code == 422, resp.text
    assert "mixes embedding setups" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_no_cross_tenant_hits(container, client, mock):
    """Search is tenant-scoped end to end: a run in the default tenant with
    a binding over ITS datasets never renders another tenant's chunks."""
    container.settings.llm_trace = True
    container.runtime._trace_llm = True
    await container.auth.create_tenant("t-e2e-b", "B")
    await container.knowledge.create_dataset(
        KnowledgeDataset(
            id="ds-b",
            name="b-notes",
            embedding_config=EmbeddingConfig(**EMBED),
        ),
        tenant_id="t-e2e-b",
    )
    own = await _dataset(client, "own")
    await _ingest(client, own["id"], "own.md", "the own-tenant fact is orange")
    mock.add_turn(turn("orange"))
    agent = await _make(client, "kb-own", knowledge={"datasets": [own["id"]], "top_k": 2})
    run_id = (
        await client.post(f"/v1/agents/{agent['definition']['id']}/run", json={"input": "?"})
    ).json()["run_id"]
    row = await _await_terminal(client, run_id)
    assert row["status"] == "succeeded"
    trace = (await client.get(f"/v1/executions/{run_id}/llm-trace")).json()
    rendered = trace["entries"][0]["request"]["messages"][0]["content"]
    assert "[own.md]" in rendered
    assert "b-notes" not in rendered
