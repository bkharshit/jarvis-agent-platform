"""Workflow routes end-to-end (S6, ADR 0015): CRUD + pin-at-publish (D42),
publish lints, the queued run/stream through the shared helpers (D41), node
events on the wire (D43), the pause-inside-node composition (ADR 0015 §7),
and tenancy parity with agents."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from jarvis.domain.agent import (
    AgentDefinition,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.message import ToolCall
from jarvis.domain.workflow import (
    AgentNodeConfig,
    ConditionNodeConfig,
    ConditionOperator,
    ConditionRoute,
    ToolNodeConfig,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from jarvis.models.mock import MockModelProvider
from jarvis.models.mock import turn as mock_turn
from jarvis.security.api_keys import generate_api_key, hash_api_key, key_prefix
from jarvis.security.passwords import hash_password

pytestmark = pytest.mark.db


def _agent(name: str, *, tools: list[ToolBinding] | None = None) -> AgentDefinition:
    return AgentDefinition(
        id=str(uuid4()),
        name=name,
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        tools=tools or [],
    )


def _workflow_body(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    start: str,
    name: str | None = None,
    **kw: Any,
) -> dict:
    """The UpsertRequest payload shape (draft pins are None; publish pins)."""
    return {
        "name": name or f"wf-{uuid4().hex[:8]}",
        "nodes": [n.model_dump() for n in nodes],
        "edges": [e.model_dump() for e in edges],
        "start_node_id": start,
        **kw,
    }


def _agent_node(node_id: str, agent_id: str, template: str = "{{input}}") -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="agent",
        config=AgentNodeConfig(agent_id=agent_id, agent_version_id=None, input_template=template),
    )


async def _post_workflow(client, body: dict) -> dict:
    resp = await client.post("/v1/workflows", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _run(client, workflow_id: str, payload: dict) -> dict:
    resp = await client.post(f"/v1/workflows/{workflow_id}/run", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _events(replay: dict) -> list[dict]:
    return [e["event"] for e in replay["events"]]


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


async def _replay(client, run_id: str) -> dict:
    resp = await client.get(f"/v1/executions/{run_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- CRUD + publish -----------------------------------------------------------


@pytest.mark.db
async def test_create_publishes_and_pins_agent_nodes(client, container):
    agent = await container.agents.create(_agent("pin-me"))
    body = _workflow_body([_agent_node("a", agent.id)], [], "a")
    resp = await client.post("/v1/workflows", json=body)
    assert resp.status_code == 201, resp.text
    detail = resp.json()

    # D42: the draft's None pin is frozen to the agent's latest at publish.
    pinned = detail["definition"]["nodes"][0]["config"]["agent_version_id"]
    assert pinned is not None
    versions = await container.agents.list_versions(agent.id)
    assert pinned == versions[0].id
    assert [v["version"] for v in detail["versions"]] == [1]
    assert detail["lints"] == []


@pytest.mark.db
async def test_graph_validation_problems_are_422s(client, container):
    agent = await container.agents.create(_agent("g"))
    node = _agent_node("a", agent.id)
    # edge to an unknown node
    bad_edge = await client.post(
        "/v1/workflows",
        json=_workflow_body([node], [WorkflowEdge(from_node="a", to_node="ghost")], "a"),
    )
    assert bad_edge.status_code == 422
    assert bad_edge.json()["error"]["kind"] == "validation"
    # cycle
    second = _agent_node("b", agent.id, template="x")
    cycled = await client.post(
        "/v1/workflows",
        json=_workflow_body(
            [node, second],
            [WorkflowEdge(from_node="a", to_node="b"), WorkflowEdge(from_node="b", to_node="a")],
            "a",
        ),
    )
    assert cycled.status_code == 422
    assert "cycle" in cycled.json()["error"]["message"].lower()


@pytest.mark.db
async def test_stale_pin_lint_is_a_warning_never_a_failure(
    client, container, mock: MockModelProvider
):
    agent = await container.agents.create(_agent("stale-me"))
    detail = await _post_workflow(client, _workflow_body([_agent_node("a", agent.id)], [], "a"))
    assert detail["lints"] == []

    # a NEW agent version after publish makes the workflow's pin stale —
    # a warning on every definition response, never a failure.
    agent.strategy = StrategyConfig(type="react")
    await container.agents.update_and_publish(agent)
    refetched = await client.get(f"/v1/workflows/{detail['definition']['id']}")
    assert refetched.json()["lints"] == ["node 'a' pins a stale agent version"]

    mock.add_turn(mock_turn("ok"))
    result = await _run(client, detail["definition"]["id"], {"input": "go"})
    assert result["status"] == "succeeded"


@pytest.mark.db
async def test_publish_refuses_agent_node_without_published_version(client):
    body = _workflow_body([_agent_node("a", "ghost-agent")], [], "a")
    resp = await client.post("/v1/workflows", json=body)
    assert resp.status_code == 422
    assert "no published version" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_unreachable_node_lints(client, container):
    agent = await container.agents.create(_agent("stray"))
    nodes = [_agent_node("a", agent.id), _agent_node("stray", agent.id, template="x")]
    detail = await _post_workflow(client, _workflow_body(nodes, [], "a"))
    assert detail["lints"] == ["node 'stray' is unreachable from the start node"]


# --- runs ----------------------------------------------------------------------


@pytest.mark.db
async def test_blocking_run_walks_the_graph_and_names_the_workflow(
    client, container, mock: MockModelProvider
):
    a = await container.agents.create(_agent("wf-a"))
    b = await container.agents.create(_agent("wf-b"))
    mock.add_turn(mock_turn("A out"))
    mock.add_turn(mock_turn("B got A out"))
    nodes = [_agent_node("a", a.id), _agent_node("b", b.id, template="{{node.a}}")]
    detail = await _post_workflow(
        client,
        _workflow_body(nodes, [WorkflowEdge(from_node="a", to_node="b")], "a"),
    )
    workflow_id = detail["definition"]["id"]

    result = await _run(client, workflow_id, {"input": "hello"})
    assert result["status"] == "succeeded"
    assert result["final_message"] == "B got A out"
    # D41: the row IS an agent_execution with the workflow id as agent_id
    assert result["agent_id"] == workflow_id
    assert result["metadata"]["kind"] == "workflow"

    events = _events(await _replay(client, result["run_id"]))
    started = [(e["node_id"], e["node_type"]) for e in events if e["type"] == "node.started"]
    completed = [(e["node_id"], e["output"]) for e in events if e["type"] == "node.completed"]
    assert started == [("a", "agent"), ("b", "agent")]
    assert completed == [("a", "A out"), ("b", "B got A out")]
    # the terminal is a run-level event, never node-scoped (D43)
    assert _types(events)[-1] == "run.completed"
    seqs = [e["sequence"] for e in events]
    assert seqs == list(range(len(seqs)))

    # the executions listing names the workflow (D41)
    listing = (await client.get("/v1/executions")).json()
    assert listing["names"][workflow_id] == detail["definition"]["name"]
    row_detail = (await client.get(f"/v1/executions/{result['run_id']}")).json()
    assert row_detail["names"] == {workflow_id: detail["definition"]["name"]}


@pytest.mark.db
async def test_stream_frames_node_events(client, container, mock: MockModelProvider):
    from tests.integration.conftest import parse_sse

    a = await container.agents.create(_agent("stream-a"))
    mock.add_turn(mock_turn("streamed out"))
    detail = await _post_workflow(client, _workflow_body([_agent_node("a", a.id)], [], "a"))

    resp = await client.post(
        f"/v1/workflows/{detail['definition']['id']}/stream", json={"input": "hi"}
    )
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    kinds = [data["type"] for _i, _ev, data in frames]
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.completed"
    node_started = [data for _i, _ev, data in frames if data["type"] == "node.started"]
    assert len(node_started) == 1
    assert node_started[0]["node_id"] == "a"
    assert node_started[0]["node_type"] == "agent"


@pytest.mark.db
async def test_pause_inside_node_then_resume_continues_the_walk(
    client, container, mock: MockModelProvider
):
    """ADR 0015 §7: human-in-the-loop composes — the pause inside agent node
    'a' ends the segment with the node named; resuming finishes 'a' and the
    walk continues to 'b'."""
    gated = _agent(
        "wf-gated",
        tools=[ToolBinding(name="calculator", config={"requires_approval": True})],
    )
    a = await container.agents.create(gated)
    b = await container.agents.create(_agent("wf-after"))
    mock.add_turn(
        mock_turn(
            tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})]
        )
    )
    mock.add_turn(mock_turn("A done"))
    mock.add_turn(mock_turn("B done"))
    nodes = [_agent_node("a", a.id), _agent_node("b", b.id, template="{{node.a}}")]
    detail = await _post_workflow(
        client, _workflow_body(nodes, [WorkflowEdge(from_node="a", to_node="b")], "a")
    )

    result = await _run(client, detail["definition"]["id"], {"input": "compute"})
    run_id = result["run_id"]
    assert result["status"] == "awaiting_input"
    assert result["finished_at"] is None

    events = _events(await _replay(client, run_id))
    # the pause frame carries the paused node's node_id (D43)
    pause = [e for e in events if e["type"] == "run.awaiting_input"]
    assert len(pause) == 1
    assert pause[0]["node_id"] == "a"
    assert "node.completed" not in _types(events)

    resumed = await client.post(f"/v1/executions/{run_id}/resume", json={"tool_approval": True})
    assert resumed.status_code == 200, resumed.text
    final = resumed.json()
    assert final["status"] == "succeeded"
    assert final["final_message"] == "B done"

    events = _events(await _replay(client, run_id))
    completed = [e["node_id"] for e in events if e["type"] == "node.completed"]
    assert completed == ["a", "b"]  # the walk continued past the resumed node
    assert _types(events).count("run.awaiting_input") == 1
    seqs = [e["sequence"] for e in events]
    assert seqs == list(range(len(seqs)))


@pytest.mark.db
async def test_condition_node_routes_on_upstream_output(client, container, mock: MockModelProvider):
    a = await container.agents.create(_agent("cond-a"))
    yes_node = await container.agents.create(_agent("cond-yes"))
    fallback = await container.agents.create(_agent("cond-fallback"))
    mock.add_turn(mock_turn("plain refusal"))  # node a
    mock.add_turn(mock_turn("fallback done"))  # the else branch's model call
    nodes = [
        _agent_node("a", a.id),
        WorkflowNode(
            id="cond",
            type="condition",
            config=ConditionNodeConfig(
                routes=[
                    ConditionRoute(
                        when=ConditionOperator(operator="contains", value="YES"), to_node="yes"
                    )
                ],
                else_node="fallback",
            ),
        ),
        _agent_node("yes", yes_node.id, template="{{node.a}}"),
        _agent_node("fallback", fallback.id, template="{{node.a}}"),
    ]
    detail = await _post_workflow(
        client, _workflow_body(nodes, [WorkflowEdge(from_node="a", to_node="cond")], "a")
    )
    result = await _run(client, detail["definition"]["id"], {"input": "decide"})
    assert result["status"] == "succeeded"
    events = _events(await _replay(client, result["run_id"]))
    started = [e["node_id"] for e in events if e["type"] == "node.started"]
    assert "yes" not in started  # the route went to the else branch
    assert "fallback" in started
    assert result["final_message"] == "fallback done"


@pytest.mark.db
async def test_tool_node_executes_and_templates_arguments(
    client, container, mock: MockModelProvider
):
    nodes = [
        WorkflowNode(
            id="t",
            type="tool",
            config=ToolNodeConfig(
                binding=ToolBinding(name="calculator", config={}),
                arguments={"expression": "{{input}}"},
            ),
        )
    ]
    detail = await _post_workflow(client, _workflow_body(nodes, [], "t"))
    result = await _run(client, detail["definition"]["id"], {"input": "2+2"})
    assert result["status"] == "succeeded"
    events = _events(await _replay(client, result["run_id"]))
    completed = [e for e in events if e["type"] == "node.completed"]
    assert completed[0]["node_id"] == "t"
    assert completed[0]["node_type"] == "tool"
    assert completed[0]["output"] == "4"


@pytest.mark.db
async def test_node_execution_cap_is_a_persisted_failure(
    client, container, mock: MockModelProvider
):
    a = await container.agents.create(_agent("cap-a"))
    b = await container.agents.create(_agent("cap-b"))
    mock.add_turn(mock_turn("one"))
    nodes = [_agent_node("a", a.id), _agent_node("b", b.id, template="x")]
    detail = await _post_workflow(
        client,
        _workflow_body(
            nodes, [WorkflowEdge(from_node="a", to_node="b")], "a", max_node_executions=1
        ),
    )
    result = await _run(client, detail["definition"]["id"], {"input": "go"})
    assert result["status"] == "failed"
    assert "max_node_executions=1" in result["error"]
    events = _events(await _replay(client, result["run_id"]))
    assert _types(events).count("run.failed") == 1
    assert "node.completed" in _types(events)  # node a finished before the cap


@pytest.mark.db
async def test_delete_refused_with_executions(client, container, mock: MockModelProvider):
    a = await container.agents.create(_agent("del-a"))
    mock.add_turn(mock_turn("bye"))
    body = _workflow_body([_agent_node("a", a.id)], [], "a")
    detail = await _post_workflow(client, body)
    workflow_id = detail["definition"]["id"]
    result = await _run(client, workflow_id, {"input": "hi"})
    assert result["status"] == "succeeded"

    refused = await client.delete(f"/v1/workflows/{workflow_id}")
    assert refused.status_code == 409

    # without executions, deletion succeeds (a fresh name — names are unique)
    fresh = await _post_workflow(client, {**body, "name": f"wf-{uuid4().hex[:8]}"})
    gone = await client.delete(f"/v1/workflows/{fresh['definition']['id']}")
    assert gone.status_code == 204


@pytest.mark.db
async def test_unknown_workflow_404(client):
    resp = await client.post("/v1/workflows/nope/run", json={"input": "hi"})
    assert resp.status_code == 404
    assert resp.json()["error"]["kind"] == "not_found"


# --- tenancy --------------------------------------------------------------------


async def _tenant_bearer(container, tenant_id: str) -> str:
    """Tenant + owner + API key — returns the Bearer plaintext."""
    await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")
    user = await container.auth.create_user(
        tenant_id=tenant_id,
        email=f"owner@{tenant_id}.test",
        password_hash=hash_password("pw"),
        role="owner",
    )
    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id=tenant_id,
        user_id=user.id,
        name="e2e",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    return plaintext


@pytest.mark.db
async def test_foreign_tenant_workflow_is_404(client, container, mock: MockModelProvider):
    bearer = await _tenant_bearer(container, "wften")
    headers = {"Authorization": f"Bearer {bearer}"}
    a = await container.agents.create(_agent("ten-a"))
    mock.add_turn(mock_turn("tenant out"))
    definition = WorkflowDefinition(
        id=str(uuid4()),
        name=f"ten-wf-{uuid4().hex[:6]}",
        nodes=[_agent_node("a", a.id)],
        start_node_id="a",
    )
    await container.workflows.create(definition, tenant_id="wften")

    # the owning tenant sees and runs it
    owned = await client.get(f"/v1/workflows/{definition.id}", headers=headers)
    assert owned.status_code == 200
    run = await client.post(
        f"/v1/workflows/{definition.id}/run", json={"input": "hi"}, headers=headers
    )
    assert run.status_code == 200

    # a foreign principal gets a plain 404 — no existence leak
    foreign_bearer = await _tenant_bearer(container, "wften-b")
    foreign = {"Authorization": f"Bearer {foreign_bearer}"}
    assert (await client.get(f"/v1/workflows/{definition.id}", headers=foreign)).status_code == 404
    denied = await client.post(
        f"/v1/workflows/{definition.id}/run", json={"input": "hi"}, headers=foreign
    )
    assert denied.status_code == 404
