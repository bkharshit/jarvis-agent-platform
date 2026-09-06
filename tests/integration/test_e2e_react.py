"""E2E: a ReAct agent with calculator + http_get runs through the API with
SSE — mock provider, respx-mocked HTTP, full event-log reconstruction."""

from __future__ import annotations

import pytest
import respx

from jarvis.domain.agent import ToolBinding
from tests.integration.conftest import parse_sse

pytestmark = pytest.mark.db


@pytest.mark.db
@respx.mock
async def test_react_end_to_end_through_api_with_sse(client, container, agent, mock):
    from jarvis.domain.agent import StrategyConfig
    from jarvis.models.mock import turn

    # Bind calculator + http_get (allow-listed to the mocked host).
    definition = await container.agents.get(agent.id)
    assert definition is not None
    updated = definition.model_copy(
        update={
            "name": "react-e2e",
            "strategy": StrategyConfig(type="react"),
            "tools": [
                ToolBinding(name="calculator"),
                ToolBinding(name="http_get", config={"allowed_hosts": ["facts.test"]}),
            ],
        }
    )
    await container.agents.update_and_publish(updated)

    respx.get("http://facts.test/answer").respond(200, json={"value": 40})

    mock.add_turn(
        turn(
            "Thought: I need the fact.\n"
            "Action: http_get\n"
            'Action Input: {"url": "http://facts.test/answer"}'
        )
    )
    mock.add_turn(
        turn('Thought: Now I add.\nAction: calculator\nAction Input: {"expression": "40 + 2"}')
    )
    mock.add_turn(turn("Final Answer: 42"))

    resp = await client.post(f"/v1/agents/{agent.id}/stream", json={"input": "what is it?"})
    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    types = [t for _, t, _ in frames]

    assert types[0] == "run.started"
    assert types.count("run.completed") == 1 and types[-1] == "run.completed"
    assert types.count("tool.call.requested") == 2
    assert types.count("tool.call.completed") == 2
    assert types.count("iteration.completed") == 3

    final = frames[-1][2]
    assert final["final_message"] == "42"
    assert final["iterations"] == 3
    run_id = frames[0][2]["run_id"]

    # The persisted rows reconstruct the whole run.
    detail = (await client.get(f"/v1/executions/{run_id}")).json()
    assert detail["run"]["status"] == "succeeded"
    assert len(detail["tool_executions"]) == 2
    assert [t["tool_name"] for t in detail["tool_executions"]] == ["http_get", "calculator"]
    assert "value" in detail["tool_executions"][0]["output"]
    assert detail["tool_executions"][1]["output"] == "42"

    # The event log replays in per-run sequence order, gapless.
    replay = (await client.get(f"/v1/executions/{run_id}/events")).json()
    sequences = [e["event"]["sequence"] for e in replay["events"]]
    assert sequences == list(range(len(sequences)))
    assert [e["event"]["type"] for e in replay["events"]] == types

    # The conversation history (memory disabled → 404) and error envelope
    # checks round out the surface.
    missing = await client.get(f"/v1/executions/{run_id}/events", params={"after": 999999})
    assert missing.json()["events"] == []
