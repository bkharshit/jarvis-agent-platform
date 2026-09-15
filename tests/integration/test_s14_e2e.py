"""S14 (ADR 0018 §6/§7) end-to-end over HTTP: a mock supervisor delegating
through real `sub__` bindings, child failure honesty, parent cancellation,
handoff chains, and the derived API facts (parent_run_id filter, detail
children, capabilities flags, SSE keep-alive).

Order-independence note (the S11 gotcha): parents block on their children,
so the mock FIFO is deterministic for scripted parent turns — but concurrent
SIBLINGS (two delegations in one batch) pop turns in an arbitrary order.
Every scenario below therefore gives sibling children the default reply, or
orders them through the blocking chain.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.models.mock import turn
from jarvis.security import generate_api_key, hash_api_key, key_prefix
from jarvis.tools.base import BaseTool

pytestmark = pytest.mark.db

CREATE = {
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


async def _make(client, name: str, **overrides) -> tuple[str, str]:
    """Create + publish an agent; returns (agent_id, pinned version id)."""
    body = {"name": name, **CREATE, **overrides}
    resp = await client.post("/v1/agents", json=body)
    assert resp.status_code == 201, resp.text
    detail = resp.json()
    return detail["definition"]["id"], detail["versions"][0]["id"]


async def _run(client, agent_id: str, body: dict[str, Any] | None = None) -> dict:
    resp = await client.post(f"/v1/agents/{agent_id}/run", json=body or {"input": "go"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _list(client, **params: Any) -> list[dict]:
    resp = await client.get("/v1/executions", params=params or None)
    assert resp.status_code == 200
    return resp.json()["items"]


async def _await_terminal(client, run_id: str) -> dict:
    """A handoff successor runs concurrently with the predecessor's blocking
    /run — poll the row to a terminal state before asserting on its tree."""
    for _ in range(200):
        resp = await client.get(f"/v1/executions/{run_id}")
        assert resp.status_code == 200
        row = resp.json()["run"]
        if row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal state")


# --- supervisor → two sub-agents (D52) ------------------------------------------


async def test_supervisor_delegates_to_two_sub_agents(client, mock):
    alpha_id, alpha_version = await _make(client, "alpha")
    beta_id, beta_version = await _make(client, "beta")
    sup_id, _ = await _make(
        client,
        "supervisor",
        tools=[
            {"name": "sub__alpha", "config": {"agent_id": alpha_id}},
            {"name": "sub__beta", "config": {"agent_id": beta_id}},
        ],
    )

    # ONE batch delegating to both sub-agents: the siblings run concurrently
    # and take the default reply (the FIFO note in the module docstring) —
    # the parent's follow-up turn is unscripted for the same reason
    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(id="c1", name="sub__alpha", arguments={"task": "count the alpha things"}),
                ToolCall(id="c2", name="sub__beta", arguments={"task": "count the beta things"}),
            ]
        )
    )

    parent = await _run(client, sup_id)
    run_id = parent["run_id"]
    assert parent["status"] == "succeeded"
    assert parent["final_message"] == "This is a mock response."

    # the children are ordinary sibling runs linked by parent_run_id (D53)
    children = await _list(client, parent_run_id=run_id)
    assert {c["agent_id"] for c in children} == {alpha_id, beta_id}
    assert {c["input"] for c in children} == {"count the alpha things", "count the beta things"}
    for child in children:
        assert child["status"] == "succeeded"
        assert child["trace_id"] == parent["trace_id"]  # one trace across the tree
        assert child["session_id"] is None  # D52: a child gets no session
        assert child["parent_run_id"] == run_id
        assert child["metadata"]["sub_agent"] == {"depth": 1}
        # D51: the server-stamped pin pinned the child's frozen version
        expected_version = alpha_version if child["agent_id"] == alpha_id else beta_version
        assert child["agent_version_id"] == expected_version

    # detail carries the children and their display names (ADR 0018 §7)
    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert {c["run_id"] for c in detail["children"]} == {c["run_id"] for c in children}
    assert set(detail["names"]) >= {alpha_id, beta_id}

    # D54: each delegation reports the child's usage through the fold channel
    sub_results = [t for t in detail["tool_executions"] if t["tool_name"].startswith("sub__")]
    assert len(sub_results) == 2
    for result in sub_results:
        assert result["is_error"] is False
        assert result["output"] == "This is a mock response."  # the child's final message
        assert result["metadata"]["usage"]["input_tokens"] == 10  # the mock's default turn


async def test_child_failure_is_an_honest_error_result(client, mock):
    worker_id, _ = await _make(client, "fragile")
    sup_id, _ = await _make(
        client, "sup", tools=[{"name": "sub__fragile", "config": {"agent_id": worker_id}}]
    )

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="sub__fragile", arguments={"task": "doomed"})])
    )
    mock.add_turn(turn(error=RuntimeError("exploded")))  # the CHILD's model call fails
    mock.add_turn(turn("recovering without the child"))

    parent = await _run(client, sup_id)
    # the parent survives — a child failure is a recoverable error result
    assert parent["status"] == "succeeded"
    assert parent["final_message"] == "recovering without the child"

    children = await _list(client, parent_run_id=parent["run_id"])
    assert [c["status"] for c in children] == ["failed"]

    detail = (await client.get(f"/v1/executions/{parent['run_id']}")).json()
    sub_result = next(t for t in detail["tool_executions"] if t["tool_name"] == "sub__fragile")
    assert sub_result["is_error"] is True
    assert "child run" in sub_result["output"] and "failed" in sub_result["output"]


# --- cancellation (D52/D54) ------------------------------------------------------


class _Sleeper(BaseTool):
    """A tool that blocks for a fixed time — keeps the blocked-parent window
    deterministically wide enough to cancel inside."""

    def __init__(self, seconds: float) -> None:
        super().__init__(
            ToolDescriptor(
                name="sleeper",
                description="Sleeps, then returns — simulates a long tool call.",
                parameters={"type": "object", "properties": {}},
            )
        )
        self._seconds = seconds

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        await asyncio.sleep(self._seconds)
        return "awake"


async def _wait_for_running_child(client, worker_id: str) -> dict:
    for _ in range(100):
        rows = await _list(client, agent_id=worker_id)
        if rows and rows[0]["status"] == "running":
            return rows[0]
        await asyncio.sleep(0.05)
    raise AssertionError("child never started")


async def test_cancel_parent_mid_child_ends_both(client, container, mock):
    container.tools.register(_Sleeper(2.0))
    worker_id, _ = await _make(client, "sleeper-agent", tools=[{"name": "sleeper"}])
    sup_id, _ = await _make(
        client,
        "sup",
        tools=[{"name": "sub__sleeper_agent", "config": {"agent_id": worker_id}}],
    )

    mock.add_turn(
        turn(
            tool_calls=[ToolCall(id="c1", name="sub__sleeper_agent", arguments={"task": "slowly"})]
        )
    )
    mock.add_turn(turn(tool_calls=[ToolCall(id="c2", name="sleeper", arguments={})]))
    mock.add_turn(turn("never reached unless the cancel was lost"))

    run_task = asyncio.create_task(
        client.post(f"/v1/agents/{sup_id}/run", json={"input": "delegate and wait"})
    )
    child = await _wait_for_running_child(client, worker_id)
    parent_run_id = child["parent_run_id"]
    assert parent_run_id is not None

    resp = await client.post(f"/v1/executions/{parent_run_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True

    # the parent's blocked tool is woken by the child's terminal: cancel the
    # child too (the cooperative parent can't interrupt a sleeping child,
    # D6) — both ends land cancelled
    resp = await client.post(f"/v1/executions/{child['run_id']}/cancel")
    assert resp.status_code == 200

    parent = (await run_task).json()
    assert parent["status"] == "cancelled"

    rows = await _list(client, agent_id=worker_id)
    assert rows[0]["status"] == "cancelled"


# --- handoffs (D56) --------------------------------------------------------------


async def test_handoff_chain_transfers_transcript_and_shares_trace(client, mock):
    c_id, _ = await _make(client, "relay-c")
    b_id, _ = await _make(
        client, "relay-b", tools=[{"name": "handoff__relay-c", "config": {"agent_id": c_id}}]
    )
    a_id, _ = await _make(
        client, "relay-a", tools=[{"name": "handoff__relay-b", "config": {"agent_id": b_id}}]
    )

    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(
                    id="h1", name="handoff__relay-b", arguments={"note": "take over the relay"}
                )
            ]
        )
    )
    mock.add_turn(
        turn(
            tool_calls=[ToolCall(id="h2", name="handoff__relay-c", arguments={"note": "finish it"})]
        )
    )
    # the last agent in the chain answers with the default reply

    first = await _run(client, a_id, {"input": "start the relay", "session_id": "relay-sess"})
    assert first["status"] == "succeeded"
    a_run = first["run_id"]

    detail = (await client.get(f"/v1/executions/{a_run}")).json()
    fired = next(t for t in detail["tool_executions"] if t["tool_name"] == "handoff__relay-b")
    assert fired["output"].startswith("Handed off to relay-b (run ")
    assert fired["metadata"]["kind"] == "handoff"
    successor_b = fired["metadata"]["successor_run_id"]

    # the successor is an ordinary run sharing the predecessor's trace AND
    # session, parent-linked, carrying the note and the rendered transcript
    rows = await _list(client, parent_run_id=a_run)
    assert [r["run_id"] for r in rows] == [successor_b]
    b_run = rows[0]
    assert b_run["trace_id"] == first["trace_id"]
    assert b_run["session_id"] == "relay-sess"
    assert b_run["metadata"]["handoff"] == {"note": "take over the relay"}
    # D56: depth rides through the chain UNCHANGED — a handoff cannot
    # launder the sub-agent depth cap
    assert b_run["metadata"]["sub_agent"] == {"depth": 0}
    assert "[user] start the relay" in b_run["input"]
    assert "[handoff note] take over the relay" in b_run["input"]

    # chain step two: relay-b handed off to relay-c the same way — the
    # successor runs concurrently with the predecessor, so wait for it
    assert (await _await_terminal(client, successor_b))["status"] == "succeeded"
    c_rows = await _list(client, parent_run_id=successor_b)
    assert len(c_rows) == 1
    c_run = await _await_terminal(client, c_rows[0]["run_id"])
    assert c_run["trace_id"] == first["trace_id"]  # one trace across the chain
    assert c_run["session_id"] == "relay-sess"
    assert c_run["metadata"]["handoff"] == {"note": "finish it"}
    assert c_run["status"] == "succeeded"


async def test_handoff_without_note_omits_the_note_block(client, mock):
    worker_id, _ = await _make(client, "next-up")
    sup_id, _ = await _make(
        client, "relay", tools=[{"name": "handoff__next_up", "config": {"agent_id": worker_id}}]
    )

    mock.add_turn(turn(tool_calls=[ToolCall(id="h1", name="handoff__next_up", arguments={})]))

    first = await _run(client, sup_id, {"input": "your call"})
    assert first["status"] == "succeeded"
    rows = await _list(client, parent_run_id=first["run_id"])
    assert len(rows) == 1
    assert rows[0]["metadata"]["handoff"] == {"note": None}
    assert "[handoff note]" not in rows[0]["input"]


# --- derived API facts (ADR 0018 §7) ---------------------------------------------


async def _other_tenant_key(container) -> dict[str, str]:
    """A tenant-b API key — the tenancy probe of test_api_auth."""
    await container.auth.create_tenant("tenant-b", "Tenant b")
    user = await container.auth.create_user(
        tenant_id="tenant-b",
        email="peek-children@example.com",
        password_hash="x",
        role="member",
    )
    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id="tenant-b",
        user_id=user.id,
        name="test",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    return {"Authorization": f"Bearer {plaintext}"}


async def test_children_are_tenant_scoped(client, container, mock):
    worker_id, _ = await _make(client, "scoped-worker")
    sup_id, _ = await _make(
        client,
        "scoped-sup",
        tools=[{"name": "sub__scoped_worker", "config": {"agent_id": worker_id}}],
    )
    mock.add_turn(
        turn(
            tool_calls=[ToolCall(id="c1", name="sub__scoped_worker", arguments={"task": "peek me"})]
        )
    )
    mock.add_turn(turn("done"))

    parent = await _run(client, sup_id)
    children = await _list(client, parent_run_id=parent["run_id"])
    assert len(children) == 1
    headers = await _other_tenant_key(container)

    # a foreign tenant cannot enumerate the children nor read one (no leak)
    foreign = await client.get(
        "/v1/executions", params={"parent_run_id": parent["run_id"]}, headers=headers
    )
    assert foreign.json()["items"] == []
    assert (
        await client.get(f"/v1/executions/{children[0]['run_id']}", headers=headers)
    ).status_code == 404
    parent_read = await client.get(f"/v1/executions/{parent['run_id']}", headers=headers)
    assert parent_read.status_code == 404

    # the owning tenant still sees the pair
    assert len(await _list(client, parent_run_id=parent["run_id"])) == 1


async def test_capabilities_report_sub_agents(client):
    resp = await client.get("/v1/capabilities")
    assert resp.status_code == 200
    sections = resp.json()["sections"]
    assert sections["agents"]["detail"]["sub_agents"] is True
    assert sections["executions"]["detail"]["sub_agents"] is True


async def test_stream_emits_keepalive_pings_during_quiet_periods(
    client, container, mock, monkeypatch
):
    """While the parent blocks on a sleeping child its SSE stream carries
    only PING comment frames — the idle-timeout keep-alive (S14)."""
    from jarvis.api.routes import _run_routes

    monkeypatch.setattr(_run_routes, "PING_AFTER_SECONDS", 0.05)

    container.tools.register(_Sleeper(0.6))
    worker_id, _ = await _make(client, "ping-worker", tools=[{"name": "sleeper"}])
    sup_id, _ = await _make(
        client,
        "ping-sup",
        tools=[{"name": "sub__ping_worker", "config": {"agent_id": worker_id}}],
    )

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="sub__ping_worker", arguments={"task": "nap"})])
    )
    mock.add_turn(turn(tool_calls=[ToolCall(id="c2", name="sleeper", arguments={})]))
    mock.add_turn(turn("woke and finished"))

    resp = await client.post(f"/v1/agents/{sup_id}/stream", json={"input": "delegate quietly"})
    assert resp.status_code == 200
    body = resp.text
    pings = re.findall(r"^: keep-alive$", body, flags=re.MULTILINE)
    assert len(pings) >= 3  # ~0.6s of quiet at a 0.05s cadence
    assert "run.completed" in body
