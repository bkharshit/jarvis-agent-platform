"""Blocking run through the API: identical semantics to the CLI, rows fully
reconstruct the run. S10: a pause ends the segment like a terminal — the
blocking route returns the awaiting_input row and /executions/{id}/resume
finishes the run."""

from __future__ import annotations

from uuid import uuid4

import pytest

from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.message import ToolCall

pytestmark = pytest.mark.db


def _gated_agent() -> AgentDefinition:
    return AgentDefinition(
        id=str(uuid4()),
        name=f"gate-{uuid4().hex[:8]}",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        tools=[ToolBinding(name="calculator", config={"requires_approval": True})],
    )


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


@pytest.mark.db
async def test_blocking_run_stops_at_pause_then_resume_completes(client, container, mock):
    from jarvis.models.mock import turn

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})])
    )
    mock.add_turn(turn("42 it is"))

    resp = await client.post(
        f"/v1/agents/{(await container.agents.create(_gated_agent())).id}/run",
        json={"input": "compute"},
    )
    assert resp.status_code == 200
    result = resp.json()
    run_id = result["run_id"]
    assert result["status"] == "awaiting_input"  # a pause is a segment end, not a failure
    assert result["finished_at"] is None

    # the pause event is durable and names the gated call
    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    pause = [e["event"] for e in events if e["event"]["type"] == "run.awaiting_input"]
    assert len(pause) == 1
    assert [c["id"] for c in pause[0]["pending_calls"]] == ["c1"]
    assert "tool.call.started" not in [e["event"]["type"] for e in events]

    # resuming with the approval completes the run
    resumed = await client.post(f"/v1/executions/{run_id}/resume", json={"tool_approval": True})
    assert resumed.status_code == 200
    final = resumed.json()
    assert final["status"] == "succeeded"
    assert final["final_message"] == "42 it is"

    # a second resume is a 409 — the run is no longer awaiting
    late = await client.post(f"/v1/executions/{run_id}/resume", json={"content": "hi"})
    assert late.status_code == 409


@pytest.mark.db
async def test_resume_content_answer_completes(client, container):
    """ask_human pause → content resume. Builtin strategies never ask, so a
    scripted strategy is registered for this test."""
    from jarvis.domain.message import Message
    from jarvis.ports.strategy import AskHumanStep, FinishStep

    class _AskOnceStrategy:
        name = "ask_once"

        def __init__(self) -> None:
            self.calls = 0

        async def step(self, ctx, messages, client, tools, sink):
            self.calls += 1
            if self.calls == 1:
                return AskHumanStep(
                    assistant_message=Message(role="assistant", content="what is your name?"),
                    question="what is your name?",
                )
            return FinishStep(
                assistant_message=Message(
                    role="assistant", content=f"hello {messages[-1].content}"
                ),
                finish_reason="stop",
            )

    # StrategyConfig.type is a closed Literal — swap the "function_calling"
    # entry for the test's strategy (registry lookup happens at run time).
    container.strategies._strategies["function_calling"] = _AskOnceStrategy()
    definition = AgentDefinition(
        id=str(uuid4()),
        name=f"ask-{uuid4().hex[:8]}",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
    )
    await container.agents.create(definition)

    resp = await client.post(f"/v1/agents/{definition.id}/run", json={"input": "hi"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "awaiting_input"
    run_id = resp.json()["run_id"]

    resumed = await client.post(f"/v1/executions/{run_id}/resume", json={"content": "Harshit"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"
    messages = (await client.get(f"/v1/executions/{run_id}")).json()["messages"]
    assert "hello Harshit" in [m["content"] for m in messages]


@pytest.mark.db
async def test_resume_rejects_unknown_run_and_bad_bodies(client, agent, mock):
    from jarvis.models.mock import turn

    resp = await client.post("/v1/executions/nope/resume", json={"content": "hi"})
    assert resp.status_code == 404

    mock.add_turn(turn("ok"))
    run = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "hi"})
    run_id = run.json()["run_id"]
    for body in (
        {"content": "hi", "tool_approval": True},
        {"content": "hi", "decisions": {"c1": True}},
        {},
        {"tool_approval": None},
        {"decisions": {}},
    ):
        bad = await client.post(f"/v1/executions/{run_id}/resume", json=body)
        assert bad.status_code == 422, body
    # a succeeded run is not awaiting input
    late = await client.post(f"/v1/executions/{run_id}/resume", json={"content": "hi"})
    assert late.status_code == 409


@pytest.mark.db
async def test_resume_decisions_pause_mixed_outcomes(client, container, mock):
    """ADR 0011: per-call verdicts — the approved call executes, the declined
    one closes with a refusal message and never ran."""
    from jarvis.models.mock import turn

    mock.add_turn(
        turn(
            tool_calls=[
                ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"}),
                ToolCall(id="c2", name="calculator", arguments={"expression": "2+2"}),
            ]
        )
    )
    mock.add_turn(turn("done"))
    resp = await client.post(
        f"/v1/agents/{(await container.agents.create(_gated_agent())).id}/run",
        json={"input": "compute"},
    )
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "awaiting_input"

    resumed = await client.post(
        f"/v1/executions/{run_id}/resume", json={"decisions": {"c1": True, "c2": False}}
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"

    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    by_call = {m["tool_call_id"]: m["content"] for m in detail["messages"] if m["role"] == "tool"}
    assert by_call["c1"] != "user declined execution"  # executed
    assert by_call["c2"] == "user declined execution"  # refused, never ran

    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    started = [
        e["event"]["tool_call_id"] for e in events if e["event"]["type"] == "tool.call.started"
    ]
    assert started == ["c1"]
    declined = [
        e["event"]["tool_call_id"] for e in events if e["event"]["type"] == "tool.call.declined"
    ]
    assert declined == ["c2"]  # the decision is durable (ADR 0011 §3)
    seqs = [e["event"]["sequence"] for e in events]
    assert seqs == list(range(len(seqs)))


@pytest.mark.db
async def test_cancel_paused_run_finishes_directly(client, container, mock):
    """ADR 0010 §6: no heartbeat holds a paused run — the cancel route
    appends the terminal itself, immediately."""
    from jarvis.models.mock import turn

    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "1"})])
    )
    resp = await client.post(
        f"/v1/agents/{(await container.agents.create(_gated_agent())).id}/run",
        json={"input": "compute"},
    )
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "awaiting_input"

    cancelled = await client.post(f"/v1/executions/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    events = (await client.get(f"/v1/executions/{run_id}/events")).json()["events"]
    types = [e["event"]["type"] for e in events]
    assert types.count("run.cancelled") == 1  # exactly one terminal
    assert [e["event"]["sequence"] for e in events] == list(range(len(events)))
    # idempotent: cancelling again is a no-op reporting the final status
    again = await client.post(f"/v1/executions/{run_id}/cancel")
    assert again.json()["cancelled"] is False and again.json()["status"] == "cancelled"
