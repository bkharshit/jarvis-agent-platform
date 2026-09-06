"""Blocking run through the API: identical semantics to the CLI, rows fully
reconstruct the run."""

from __future__ import annotations

import pytest

from jarvis.domain.message import ToolCall

pytestmark = pytest.mark.db


@pytest.mark.db
async def test_blocking_run_writes_rows(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "1+2"})])
    )
    mock.add_turn(turn("The answer is 3"))

    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "what is 1+2?"})
    assert resp.status_code == 200
    result = resp.json()
    run_id = result["run_id"]
    assert result["status"] == "succeeded"
    assert result["final_message"] == "The answer is 3"
    assert result["iterations"] == 2
    assert result["total_usage"]["input_tokens"] == 20  # 2 invocations x 10

    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert detail["run"]["status"] == "succeeded"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert detail["tool_executions"][0]["tool_name"] == "calculator"
    assert detail["tool_executions"][0]["output"] == "3"

    replay = (await client.get(f"/v1/executions/{run_id}/events")).json()
    events = replay["events"]
    types = [e["event"]["type"] for e in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert "tool.call.requested" in types and "tool.call.completed" in types
    assert [e["event"]["sequence"] for e in events] == list(range(len(events)))
    cursors = [e["cursor"] for e in events]
    assert cursors == sorted(cursors)


@pytest.mark.db
async def test_run_unknown_agent_404(client):
    resp = await client.post("/v1/agents/nope/run", json={"input": "hi"})
    assert resp.status_code == 404


@pytest.mark.db
async def test_run_rejects_empty_input(client, agent):
    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["kind"] == "validation"


@pytest.mark.db
async def test_variables_and_metadata_flow_through(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("ok"))
    resp = await client.post(
        f"/v1/agents/{agent.id}/run",
        json={
            "input": "hi",
            "session_id": "sess-1",
            "user_id": "u1",
            "metadata": {"origin": "test"},
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert detail["run"]["session_id"] == "sess-1"
    assert detail["run"]["trace_id"]
    transcript = await client.get(f"/v1/conversations/{agent.id}/sess-1/messages")
    # memory disabled on this agent → no conversation rows
    assert transcript.status_code == 404
