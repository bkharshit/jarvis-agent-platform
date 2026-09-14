"""Executions API: list filters, cancel semantics (idempotent, live), and
SSE replay via Accept header."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.domain.agent import ToolBinding
from jarvis.domain.auth import Principal
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.tools.base import BaseTool
from tests.integration.conftest import parse_sse

pytestmark = pytest.mark.db


class _SlowTool(BaseTool):
    """Sleeps long enough for a cancel to land mid-run. Cancellation is
    cooperative — the tool finishes its sleep, the runtime raises at the next
    checkpoint."""

    def __init__(self) -> None:
        super().__init__(
            ToolDescriptor(
                name="slow_tool",
                description="sleeps",
                parameters={"type": "object", "properties": {}},
            )
        )

    async def _execute(self, arguments: dict[str, object], context: ToolContext) -> str:
        await asyncio.sleep(3)
        return "finally"


@pytest.mark.db
async def test_cancel_queued_run_never_reaches_the_runtime(client, container, agent, mock):
    """Cancelling a queued run writes the cross-process request (ADR 0008 §6);
    the worker pops it before the runtime is ever invoked."""
    from jarvis.api.routes.agents import _queue_message, _queued_result
    from jarvis.api.schemas import RunRequest
    from jarvis.models.mock import turn

    mock.add_turn(turn("never reached"))
    definition = await container.agents.get(agent.id)
    assert definition is not None
    version = await container.agents.latest_version(agent.id)
    assert version is not None
    message = _queue_message(
        container.settings,
        "agent",
        definition.id,
        version.id,
        RunRequest(input="go"),
        Principal(tenant_id="default", mode="anonymous"),
    )
    await container.executions.create_queued_run(_queued_result(message), message)

    cancel = await client.post(f"/v1/executions/{message.run_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    for _ in range(200):  # the embedded worker fast-fails the pre-cancelled run
        detail = (await client.get(f"/v1/executions/{message.run_id}")).json()
        if detail["run"]["status"] == "cancelled":
            break
        await asyncio.sleep(0.05)
    assert detail["run"]["status"] == "cancelled"
    events = (await client.get(f"/v1/executions/{message.run_id}/events")).json()
    assert [e["event"]["type"] for e in events["events"]] == ["run.cancelled"]


@pytest.mark.db
async def test_executions_list_filters(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("one"))
    mock.add_turn(turn("two"))
    run_a = (await client.post(f"/v1/agents/{agent.id}/run", json={"input": "1"})).json()["run_id"]
    run_b = (
        await client.post(f"/v1/agents/{agent.id}/run", json={"input": "2", "session_id": "s9"})
    ).json()["run_id"]

    listed = (await client.get("/v1/executions", params={"agent_id": agent.id})).json()
    assert {r["run_id"] for r in listed["items"]} == {run_a, run_b}

    listed = (await client.get("/v1/executions", params={"status": "succeeded"})).json()
    assert {r["run_id"] for r in listed["items"]} == {run_a, run_b}

    listed = (await client.get("/v1/executions", params={"session_id": "s9"})).json()
    assert [r["run_id"] for r in listed["items"]] == [run_b]

    listed = (await client.get("/v1/executions", params={"agent_id": "other"})).json()
    assert listed["items"] == []


@pytest.mark.db
async def test_cancel_live_run_is_idempotent(client, container, agent, mock):
    from jarvis.models.mock import turn

    container.tools.register(_SlowTool())
    definition = await container.agents.get(agent.id)
    assert definition is not None
    updated = definition.model_copy(
        update={"tools": [ToolBinding(name="slow_tool")], "name": "cancel-agent"}
    )
    await container.agents.update_and_publish(updated)

    mock.add_turn(turn(tool_calls=[ToolCall(id="c1", name="slow_tool", arguments={})]))
    mock.add_turn(turn("never reached"))

    # ASGITransport buffers the whole response, so the stream can't be read
    # live — run it in the background and observe the live run through the
    # RUNNING row the runtime writes on its first event.
    stream_task = asyncio.create_task(
        client.post(f"/v1/agents/{agent.id}/stream", json={"input": "go"})
    )
    run_id: str | None = None
    for _ in range(200):  # up to 10s
        listed = (await client.get("/v1/executions", params={"status": "running"})).json()
        if listed["items"]:
            run_id = listed["items"][0]["run_id"]
            break
        await asyncio.sleep(0.05)
    assert run_id is not None

    cancel = await client.post(f"/v1/executions/{run_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json() == {"run_id": run_id, "cancelled": True, "status": "running"}

    # Drain the original stream to its terminal event, then reconnect from 0.
    frames = parse_sse((await stream_task).text)
    assert frames[-1][1] == "run.cancelled", (
        f"terminal was {frames[-1][1]}: {frames[-1][2].get('error')!r}"
    )
    assert frames[-1][2]["reason"] == "cancelled by user"

    resumed = await client.post(
        f"/v1/agents/{agent.id}/stream",
        json={"input": "go", "run_id": run_id},
        headers={"Last-Event-ID": "0"},
    )
    assert parse_sse(resumed.text)[-1][1] == "run.cancelled"

    # Idempotent: cancelling a finished run reports its state, no error.
    again = await client.post(f"/v1/executions/{run_id}/cancel")
    assert again.status_code == 200
    assert again.json() == {"run_id": run_id, "cancelled": False, "status": "cancelled"}


@pytest.mark.db
async def test_cancel_unknown_run_404(client):
    resp = await client.post("/v1/executions/missing/cancel")
    assert resp.status_code == 404


@pytest.mark.db
async def test_events_replay_json_and_sse(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("payload"))
    run_id = (await client.post(f"/v1/agents/{agent.id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]

    json_resp = await client.get(f"/v1/executions/{run_id}/events")
    assert json_resp.status_code == 200
    replay = json_resp.json()
    types = [e["event"]["type"] for e in replay["events"]]
    assert types[0] == "run.started" and types[-1] == "run.completed"

    # `after` is cursor space: everything after the first event's cursor.
    first_cursor = replay["events"][0]["cursor"]
    after = await client.get(f"/v1/executions/{run_id}/events", params={"after": first_cursor})
    after_ids = [e["cursor"] for e in after.json()["events"]]
    assert after_ids == [e["cursor"] for e in replay["events"]][1:]

    # Accept: text/event-stream frames the same events with the same cursors.
    sse_resp = await client.get(
        f"/v1/executions/{run_id}/events", headers={"Accept": "text/event-stream"}
    )
    assert sse_resp.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(sse_resp.text)
    assert [i for i, _, _ in frames] == [e["cursor"] for e in replay["events"]]
    assert frames[-1][1] == "run.completed"

    # SSE replay after a cursor.
    sse_after = await client.get(
        f"/v1/executions/{run_id}/events",
        params={"after": first_cursor},
        headers={"Accept": "text/event-stream"},
    )
    assert [i for i, _, _ in parse_sse(sse_after.text)] == after_ids


@pytest.mark.db
async def test_conversation_messages_endpoint(client, container, mock):
    from uuid import uuid4

    from jarvis.domain.agent import AgentDefinition, MemoryConfig, ModelRef, StrategyConfig
    from jarvis.domain.execution import ExecutionContext
    from jarvis.models.mock import turn

    definition = AgentDefinition(
        id=str(uuid4()),
        name="memory-endpoint-agent",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        memory=MemoryConfig(enabled=True),
    )
    await container.agents.create(definition)
    version = await container.agents.latest_version(definition.id)
    assert version is not None
    mock.add_turn(turn("first reply"))
    mock.add_turn(turn("second reply"))
    for i in range(2):
        ctx = ExecutionContext(
            run_id=f"run-conv-{i}",
            agent_id=definition.id,
            agent_version_id=version.id,
            session_id="conv-sess",
            trace_id="t",
        )
        await container.runtime.run(version, f"question {i}", ctx)

    resp = await client.get(f"/v1/conversations/{definition.id}/conv-sess/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["role"] for m in body["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert body["messages"][1]["content"] == "first reply"

    limited = await client.get(
        f"/v1/conversations/{definition.id}/conv-sess/messages", params={"limit": 2}
    )
    assert [m["content"] for m in limited.json()["messages"]] == [
        "question 1",
        "second reply",
    ]

    missing = await client.get(f"/v1/conversations/{definition.id}/nope/messages")
    assert missing.status_code == 404


# --- LLM trace route (ADR 0014) -------------------------------------------------


@pytest.mark.db
async def test_llm_trace_route_serves_the_buffered_entries(client, container, agent, mock):
    from jarvis.models.mock import turn

    # Flip the debug flag on the live container (settings default off).
    container.settings.llm_trace = True
    container.runtime._trace_llm = True
    mock.add_turn(turn("traced answer"))
    run_id = (await client.post(f"/v1/agents/{agent.id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]

    resp = await client.get(f"/v1/executions/{run_id}/llm-trace")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["method"] == "stream"
    assert entry["request"]["messages"][-1] == {
        "role": "user",
        "content": "hi",
        "tool_calls": None,
        "tool_call_id": None,
        "name": None,
        "created_at": entry["request"]["messages"][-1]["created_at"],
    }  # the ACTUAL input — the system prompt (fixture agent has none) would sit at [0]
    assert entry["response"] is not None


@pytest.mark.db
async def test_llm_trace_route_empty_is_a_200_not_an_error(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("quiet"))
    run_id = (await client.post(f"/v1/agents/{agent.id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]

    resp = await client.get(f"/v1/executions/{run_id}/llm-trace")
    assert resp.status_code == 200
    assert resp.json() == {"run_id": run_id, "entries": []}


@pytest.mark.db
async def test_llm_trace_route_unknown_run_404(client):
    resp = await client.get("/v1/executions/missing/llm-trace")
    assert resp.status_code == 404


@pytest.mark.db
async def test_llm_trace_route_is_tenant_scoped(client, container, agent, mock):
    from jarvis.models.mock import turn
    from jarvis.security import generate_api_key, hash_api_key, hash_password, key_prefix

    mock.add_turn(turn("mine"))
    run_id = (await client.post(f"/v1/agents/{agent.id}/run", json={"input": "hi"})).json()[
        "run_id"
    ]

    await container.auth.create_tenant("acme-b", "Tenant acme-b")
    user = await container.auth.create_user(
        tenant_id="acme-b",
        email="owner@acme-b.test",
        password_hash=hash_password("pw"),
        role="owner",
    )
    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id="acme-b",
        user_id=user.id,
        name="e2e",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )

    foreign = await client.get(
        f"/v1/executions/{run_id}/llm-trace", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert foreign.status_code == 404  # D29: no existence leak
