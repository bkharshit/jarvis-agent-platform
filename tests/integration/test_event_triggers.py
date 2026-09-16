"""Event triggers over the real stack (S13, ADR 0019 §5, D61): a
terminal run fires the tenant's matching event triggers through the
finish_run observer — end-to-end through the embedded worker, the
condition matrix at the repo seam, the depth-1 refusal, and the
row-metadata authority."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.execution import RunResult

pytestmark = pytest.mark.db

AGENT_BODY = {
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


async def _agent(client, name: str) -> str:
    body = {**AGENT_BODY, "name": name}
    resp = await client.post("/v1/agents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["definition"]["id"]


async def _event_trigger(client, agent_id: str, **cfg: Any) -> str:
    config = {"type": "event", "status": "any"} | cfg
    resp = await client.post(
        "/v1/triggers",
        json={
            "name": f"on-{uuid4().hex[:8]}",
            "kind": "event",
            "agent_id": agent_id,
            "config": config,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["trigger"]["id"]


async def _runs_for(client, trigger_id: str) -> list[dict]:
    return (await client.get("/v1/executions", params={"trigger_id": trigger_id})).json()["items"]


async def _wait_for_runs(client, trigger_id: str, count: int) -> list[dict]:
    for _ in range(80):
        runs = await _runs_for(client, trigger_id)
        if len(runs) >= count:
            return runs
        await asyncio.sleep(0.1)
    return await _runs_for(client, trigger_id)


async def test_succeeded_run_fires_event_trigger_end_to_end(client):
    source_id = await _agent(client, "evt-source")
    target_id = await _agent(client, "evt-target")
    trigger_id = await _event_trigger(client, target_id, status="succeeded")
    resp = await client.post(f"/v1/agents/{source_id}/run", json={"input": "finish me"})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    # the source run itself finishes through the embedded worker
    for _ in range(80):
        detail = (await client.get(f"/v1/executions/{run_id}")).json()
        if detail["run"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.1)
    assert detail["run"]["status"] == "succeeded"
    # ... and its finalize fires the event trigger's target run
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1
    fired_id = runs[0]["run_id"]
    for _ in range(80):
        runs = await _runs_for(client, trigger_id)
        if runs[0]["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.1)
    fired = runs[0]
    assert fired["run_id"] == fired_id
    assert fired["agent_id"] == target_id
    assert fired["status"] == "succeeded"  # the fired run executes end-to-end
    assert fired["metadata"]["trigger"]["trigger_id"] == trigger_id
    assert fired["metadata"]["trigger"]["depth"] == 1  # the guard's currency
    assert fired["parent_run_id"] is None  # composition, not a child run


async def test_failed_status_match_at_the_repo_seam(client, container):
    """A failed terminal fires a `status: failed` trigger; a succeeded one
    does not. The runs are driven straight through the repo (a mock
    provider failure needs no scripting here)."""
    target_id = await _agent(client, "evt-target")
    trigger_id = await _event_trigger(client, target_id, status="failed")
    result = RunResult(run_id=str(uuid4()), agent_id="evt-source", status="queued")
    await container.executions.create_run(result)
    # a succeeded finalize mismatches — nothing fires
    await container.executions.finish_run(
        result.model_copy(update={"status": "succeeded", "finished_at": datetime.now(UTC)})
    )
    assert await _runs_for(client, trigger_id) == []
    # the failed finalize matches — exactly one fire
    await container.executions.finish_run(
        result.model_copy(update={"status": "failed", "finished_at": datetime.now(UTC)})
    )
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1 and runs[0]["agent_id"] == target_id


async def test_source_agent_filter(client):
    source_id = await _agent(client, "evt-source")
    target_id = await _agent(client, "evt-target")
    trigger_id = await _event_trigger(
        client, target_id, status="any", source_agent_id="some-other-agent"
    )
    resp = await client.post(f"/v1/agents/{source_id}/run", json={"input": "hi"})
    assert resp.status_code == 200, resp.text
    await asyncio.sleep(0.5)
    assert await _runs_for(client, trigger_id) == []


async def test_depth_one_run_does_not_refire(client):
    """The guard at work: the trigger's own fired run finalizes at depth 1
    (max default 1) — its completion must NOT re-fire the trigger."""
    target_id = await _agent(client, "evt-target")
    trigger_id = await _event_trigger(client, target_id)
    resp = await client.post(f"/v1/triggers/{trigger_id}/fire")
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    for _ in range(80):
        detail = (await client.get(f"/v1/executions/{run_id}")).json()
        if detail["run"]["status"] in ("succeeded", "failed", "cancelled", "timed_out"):
            break
        await asyncio.sleep(0.1)
    assert detail["run"]["status"] == "succeeded"
    await asyncio.sleep(0.3)  # a re-fire would surface here
    runs = await _runs_for(client, trigger_id)
    assert [r["run_id"] for r in runs] == [run_id]


async def test_row_metadata_is_the_guard_authority(client, container):
    """finish_run's caller may carry contradicting metadata — the observer
    sees the result rebuilt from the ROW (D61): the persisted trigger
    provenance decides the depth guard."""
    target_id = await _agent(client, "evt-target")
    trigger_id = await _event_trigger(client, target_id)
    # a trigger-fired row (depth 1) whose finish_run caller claims NO metadata
    result = RunResult(
        run_id=str(uuid4()),
        agent_id="evt-source",
        status="queued",
        metadata={"trigger": {"trigger_id": "t-0", "kind": "event", "name": "x", "depth": 1}},
    )
    await container.executions.create_run(result)
    await container.executions.finish_run(
        result.model_copy(update={"status": "succeeded", "metadata": {}})
    )
    assert await _runs_for(client, trigger_id) == []  # the ROW's depth refused it
    # the mirror: a clean row whose caller claims depth 9 — still fires
    clean = RunResult(run_id=str(uuid4()), agent_id="evt-source", status="queued")
    await container.executions.create_run(clean)
    await container.executions.finish_run(
        clean.model_copy(
            update={
                "status": "succeeded",
                "metadata": {"trigger": {"depth": 9, "trigger_id": "t-0"}},
                "finished_at": datetime.now(UTC),
            }
        )
    )
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1


@pytest.mark.db
async def test_cancelled_via_route_fires_event_trigger(client, container, mock):
    """The cancel route finishes a paused run directly (finish_paused_run →
    finish_run) — the observer sees that terminal too."""
    from jarvis.domain.message import ToolCall
    from jarvis.models.mock import turn

    gated = AgentDefinition(
        id=str(uuid4()),
        name=f"gate-{uuid4().hex[:8]}",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        tools=[ToolBinding(name="calculator", config={"requires_approval": True})],
    )
    await container.agents.create(gated)
    target_id = await _agent(client, "evt-cancel-target")
    trigger_id = await _event_trigger(client, target_id, status="cancelled")
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "1"})])
    )
    resp = await client.post(f"/v1/agents/{gated.id}/run", json={"input": "compute"})
    assert resp.json()["status"] == "awaiting_input"
    run_id = resp.json()["run_id"]
    cancelled = await client.post(f"/v1/executions/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1 and runs[0]["agent_id"] == target_id
    assert runs[0]["metadata"]["trigger"]["trigger_id"] == trigger_id
