"""S4 acceptance e2e (roadmap §S4): an agent bound to `mcp__fixtures__add_numbers`
runs through the full HTTP surface with rows/events identical in shape to a
builtin run — /run, /stream, and the pause→decisions→resume chain. The MCP
server is a REAL subprocess (tests/fixtures/mcp/server.py over stdio); only
the model is mocked. Also proves the honest delete story: removing the
server row fails the next run's resolution as a terminal `tool` failure."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.message import ToolCall
from tests.integration.conftest import parse_sse

pytestmark = pytest.mark.db

FIXTURE_SERVER = Path(__file__).resolve().parent.parent / "fixtures" / "mcp" / "server.py"


async def _create_server(client) -> dict:
    resp = await client.post(
        "/v1/mcp/servers",
        json={
            "name": "fixtures",
            "config": {"type": "stdio", "command": sys.executable, "args": [str(FIXTURE_SERVER)]},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _mcp_agent(*, ungated: bool) -> AgentDefinition:
    config = {"requires_approval": False} if ungated else {}
    binding = ToolBinding(name="mcp__fixtures__add_numbers", config=config)
    return AgentDefinition(
        id=str(uuid4()),
        name=f"mcp-{uuid4().hex[:8]}",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        tools=[binding],
    )


@pytest.mark.db
async def test_blocking_run_through_mcp_matches_builtin_shape(client, container, mock):
    from jarvis.models.mock import turn

    await _create_server(client)
    agent = await container.agents.create(_mcp_agent(ungated=True))
    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(id="c1", name="mcp__fixtures__add_numbers", arguments={"a": 2, "b": 3})
            ]
        )
    )
    mock.add_turn(turn("The sum is 5"))

    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "add 2 and 3"})
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "succeeded"
    assert result["final_message"] == "The sum is 5"
    assert result["iterations"] == 2
    run_id = result["run_id"]

    # rows reconstruct the run exactly like a builtin tool run
    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert detail["tool_executions"][0]["tool_name"] == "mcp__fixtures__add_numbers"
    assert detail["tool_executions"][0]["output"] == "5.0"
    assert detail["tool_executions"][0]["is_error"] is False

    # events: same shape as the builtin run, gapless, durable cursors
    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    types = [e["event"]["type"] for e in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "tool.call.requested" in types and "tool.call.started" in types
    assert "tool.call.completed" in types
    assert [e["event"]["sequence"] for e in events] == list(range(len(events)))
    assert [e["cursor"] for e in events] == sorted(e["cursor"] for e in events)

    # the call actually crossed the stdio boundary: the transcript's tool
    # message carries the fixture server's text content
    tool_message = [m for m in detail["messages"] if m["role"] == "tool"][0]
    assert "5.0" in tool_message["content"]


@pytest.mark.db
async def test_stream_run_through_mcp(client, container, mock):
    from jarvis.models.mock import turn

    await _create_server(client)
    agent = await container.agents.create(_mcp_agent(ungated=True))
    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(id="c1", name="mcp__fixtures__add_numbers", arguments={"a": 10, "b": 5})
            ]
        )
    )
    mock.add_turn(turn("fifteen"))

    resp = await client.post(f"/v1/agents/{agent.id}/stream", json={"input": "add 10 and 5"})
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    types = [event for _, event, _ in frames]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "tool.call.started" in types and "tool.call.completed" in types
    terminal = [data for _, event, data in frames if event == "run.completed"]
    assert terminal[0]["final_message"] == "fifteen"


@pytest.mark.db
async def test_mcp_default_approval_pauses_then_decisions_resume(client, container, mock):
    from jarvis.models.mock import turn

    await _create_server(client)
    agent = await container.agents.create(_mcp_agent(ungated=False))
    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(id="c1", name="mcp__fixtures__add_numbers", arguments={"a": 1, "b": 1})
            ]
        )
    )
    mock.add_turn(turn("two"))

    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "add 1 and 1"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "awaiting_input"  # the MCP approval default
    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    assert "tool.call.started" not in [e["event"]["type"] for e in events]
    pause = [e["event"] for e in events if e["event"]["type"] == "run.awaiting_input"]
    assert [c["name"] for c in pause[0]["pending_calls"]] == ["mcp__fixtures__add_numbers"]

    resumed = await client.post(f"/v1/executions/{run_id}/resume", json={"decisions": {"c1": True}})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"
    assert resumed.json()["final_message"] == "two"

    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert detail["tool_executions"][0]["tool_name"] == "mcp__fixtures__add_numbers"
    assert detail["tool_executions"][0]["output"] == "2.0"
    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    seqs = [e["event"]["sequence"] for e in events]
    assert seqs == list(range(len(seqs)))  # gapless across the resume boundary
    started = [
        e["event"]["tool_call_id"] for e in events if e["event"]["type"] == "tool.call.started"
    ]
    assert started == ["c1"]  # only after the approval


@pytest.mark.db
async def test_deleted_server_fails_resolution_honestly(client, container, mock):
    """Delete the server row: snapshots keep the binding; the next run fails
    resolution as exactly one persisted terminal `tool` failure naming the
    server — no exception, no worker retry (D37 §5, D38)."""
    from jarvis.models.mock import turn

    server = await _create_server(client)
    agent = await container.agents.create(_mcp_agent(ungated=True))
    mock.add_turn(turn("never reached — resolution fails first"))

    deleted = await client.delete(f"/v1/mcp/servers/{server['id']}")
    assert deleted.status_code == 204

    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "add 2 and 2"})
    assert resp.status_code == 200  # a failed run is a valid HTTP response
    result = resp.json()
    assert result["status"] == "failed"
    assert result["error_kind"] == "tool"
    assert "fixtures" in (result["error"] or "")

    events = (await client.get(f"/v1/executions/{result['run_id']}/events")).json()["events"]
    failed = [e["event"] for e in events if e["event"]["type"] == "run.failed"]
    assert len(failed) == 1
    assert failed[0]["error_kind"] == "tool"
    assert mock.invocations == 0  # no tokens spent on an unresolvable toolset
