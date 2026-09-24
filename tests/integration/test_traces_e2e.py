"""Traces end-to-end (S7, ADR 0021 acceptance): a real queue run through
the worker produces its five span kinds in ONE trace_id (the run's
persisted trace_id, D73), a knowledge binding adds the retrieval span, a
sub-agent child shares the parent's trace, a failed run carries error
status, and a webhook's `traceparent` joins the producer's trace (D76)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from jarvis.domain.message import ToolCall
from jarvis.models.mock import turn

pytestmark = pytest.mark.db

CREATE = {
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


async def _make(client, name: str, **overrides) -> dict:
    resp = await client.post("/v1/agents", json={"name": name, **CREATE, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _run(client, agent_id: str) -> str:
    resp = await client.post(f"/v1/agents/{agent_id}/run", json={"input": "hi"})
    assert resp.status_code == 200, resp.text
    return resp.json()["run_id"]


async def _await_terminal(client, run_id: str) -> dict:
    for _ in range(200):
        resp = await client.get(f"/v1/executions/{run_id}")
        assert resp.status_code == 200, resp.text
        row = resp.json()["run"]
        if row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal state")


async def _trace_id(client, run_id: str) -> str:
    """The run row's trace_id (D73 — the id the spans table groups by)."""
    row = await _await_terminal(client, run_id)
    return row["trace_id"]


async def _detail(client, trace_id: str) -> dict:
    resp = await client.get(f"/v1/traces/{trace_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_run_produces_all_span_kinds_in_one_trace(client, mock):
    """Calculator via the real ToolRuntime: segment + iteration + llm.call
    + tool.call in one trace, parentage threaded, event cursors present."""
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})])
    )
    mock.add_turn(turn("6 times 7 is 42."))
    agent = await _make(client, "calc-agent", tools=[{"name": "calculator", "config": {}}])
    run_id = await _run(client, agent["definition"]["id"])
    trace_id = await _trace_id(client, run_id)

    assert len(trace_id) == 32 and trace_id != ""
    detail = (await client.get(f"/v1/traces/{trace_id}")).json()
    spans = detail["spans"]
    kinds = {s["kind"] for s in spans}
    assert {"run.segment", "iteration", "llm.call", "tool.call"} <= kinds
    assert all(s["trace_id"] == trace_id for s in spans)
    assert all(s["run_id"] == run_id for s in spans)
    # Every run's spans belong to the listed summary — the group view agrees.
    listed = (await client.get("/v1/traces")).json()["traces"]
    summary = next(t for t in listed if t["trace_id"] == trace_id)
    assert run_id in summary["run_ids"] and summary["span_count"] == len(spans)
    # The tool span carries the exact replay position of its ToolCallStarted.
    tool_span = next(s for s in spans if s["kind"] == "tool.call")
    assert "jarvis.event.cursor" in tool_span["attributes"]
    assert tool_span["attributes"]["tool"] == "calculator"


async def test_retrieval_span_lands_in_the_trace(client, container, mock):
    """A knowledge binding makes every fresh segment retrieve — the
    knowledge.retrieve span rides the same trace."""
    resp = await client.post(
        "/v1/knowledge/datasets",
        json={
            "name": "facts",
            "embedding_config": {
                "provider": "mock",
                "model": "mock-embedder",
                "dimension": 8,
            },
            "chunk_config": {},
        },
    )
    assert resp.status_code == 201, resp.text
    dataset_id = resp.json()["id"]
    ingest = await client.post(
        f"/v1/knowledge/datasets/{dataset_id}/documents",
        json={"name": "facts.md", "content": "the Eiffel Tower is in Paris"},
    )
    assert ingest.status_code == 201, ingest.text
    agent = await _make(client, "kb-trace", knowledge={"datasets": [dataset_id], "top_k": 2})
    run_id = await _run(client, agent["definition"]["id"])
    trace_id = await _trace_id(client, run_id)
    spans = (await client.get(f"/v1/traces/{trace_id}")).json()["spans"]
    (retrieve,) = [s for s in spans if s["kind"] == "knowledge.retrieve"]
    assert retrieve["attributes"]["datasets"] == dataset_id
    assert "chars" in retrieve["attributes"]


async def test_child_run_shares_the_parent_trace(client, mock):
    """The S14 child copies the parent's trace_id verbatim — one trace per
    multi-agent conversation (D73), visible in the spans table."""
    alpha_id = (await _make(client, "alpha"))["definition"]["id"]
    sup = await _make(
        client, "supervisor", tools=[{"name": "sub__alpha", "config": {"agent_id": alpha_id}}]
    )
    # Parent: delegate, then answer from the (default) follow-up turn; the
    # child takes the default reply (the S14 concurrency note).
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="sub__alpha", arguments={"task": "count"})])
    )
    parent_run = await _run(client, sup["definition"]["id"])
    trace_id = await _trace_id(client, parent_run)
    spans = (await client.get(f"/v1/traces/{trace_id}")).json()["spans"]
    child_run_ids = {s["run_id"] for s in spans} - {parent_run}
    assert child_run_ids, "the child's spans must land in the parent's trace"
    await _await_terminal(client, next(iter(child_run_ids)))
    row = (await client.get(f"/v1/executions/{next(iter(child_run_ids))}")).json()["run"]
    assert row["parent_run_id"] == parent_run
    assert row["trace_id"] == trace_id  # the copy is exact


async def test_failed_run_carries_error_status(client):
    """An unresolvable credential (no OPENAI_API_KEY in the test process) is
    a persisted terminal failure (D28) — the segment span reports error."""
    agent = await _make(
        client,
        "dead-model",
        model={"provider": "openai_compatible", "model": "x", "base_url": "http://127.0.0.1:9/v1"},
    )
    run_id = await _run(client, agent["definition"]["id"])
    trace_id = await _trace_id(client, run_id)  # terminal, status failed
    row = (await client.get(f"/v1/executions/{run_id}")).json()["run"]
    assert row["status"] == "failed"
    spans = (await client.get(f"/v1/traces/{trace_id}")).json()["spans"]
    statuses = {s["status"] for s in spans}
    assert "error" in statuses
    (segment,) = [s for s in spans if s["kind"] == "run.segment"]
    assert segment["status"] == "error"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_traceparent_joins_the_producers_trace(client, monkeypatch):
    """D76 inbound: the fired run carries the `traceparent`'s trace id —
    the producer's trace, not a fresh mint — and its spans land there."""
    import base64
    import os

    monkeypatch.setenv(
        "JARVIS_CREDENTIALS_MASTER_KEY",
        base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
    )
    agent = await _make(client, "hook-agent")
    created = await client.post(
        "/v1/triggers",
        json={
            "name": "trace-hook",
            "kind": "webhook",
            "agent_id": agent["definition"]["id"],
            "config": {"type": "webhook"},
        },
    )
    assert created.status_code == 201, created.text
    trigger_id, secret = created.json()["trigger"]["id"], created.json()["secret"]

    producer_trace = "e" * 32
    body = b'{"alert": "disk-full"}'
    headers = {
        "X-Jarvis-Signature": _sig(secret, body),
        "traceparent": f"00-{producer_trace}-{'f' * 16}-01",
    }
    resp = await client.post(f"/v1/triggers/webhook/{trigger_id}", content=body, headers=headers)
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]

    row = await _await_terminal(client, run_id)
    assert row["trace_id"] == producer_trace  # joined the caller's trace
    spans = (await client.get(f"/v1/traces/{producer_trace}")).json()["spans"]
    assert spans and all(s["trace_id"] == producer_trace for s in spans)
    assert all(
        s["attributes"]["trigger_id"] == trigger_id for s in spans if s["kind"] == "run.segment"
    )
