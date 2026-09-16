"""Trigger e2e + honesty paths (S13, ADR 0019 plan §8): the cron claim →
fire → worker-claims → succeed pipeline, catch-up firing ONCE, disabled
fires nothing (history intact), delete-keeps-provenance, concurrent
schedulers never double-fire, and firing failures stamp last_error.
The webhook + event e2e smokes live in test_api_triggers.py /
test_event_triggers.py (re-asserted there)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

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


async def _cron_trigger(client, agent_id: str, schedule: str = "* * * * *") -> tuple[str, str]:
    resp = await client.post(
        "/v1/triggers",
        json={
            "name": f"tick-{uuid4().hex[:8]}",
            "kind": "cron",
            "agent_id": agent_id,
            "config": {"type": "cron", "schedule": schedule},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["trigger"]["id"], resp.json()["trigger"]["name"]


async def _backdate(container, trigger_id: str, minutes_ago: float = 1.0) -> None:
    """The claim's due condition is `next_fire_at <= now` — backdate the row
    instead of waiting out a real schedule slot."""
    async with container.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE triggers SET next_fire_at = now() - make_interval(mins => :m) "
                "WHERE id = :id"
            ),
            {"id": trigger_id, "m": minutes_ago},
        )


async def _runs_for(client, trigger_id: str) -> list[dict]:
    return (await client.get("/v1/executions", params={"trigger_id": trigger_id})).json()["items"]


async def _wait_for_runs(client, trigger_id: str, count: int) -> list[dict]:
    for _ in range(80):
        runs = await _runs_for(client, trigger_id)
        if len(runs) >= count:
            return runs
        await asyncio.sleep(0.1)
    return await _runs_for(client, trigger_id)


async def _wait_terminal(client, run_id: str) -> str:
    for _ in range(80):
        detail = (await client.get(f"/v1/executions/{run_id}")).json()
        if detail["run"]["status"] not in ("queued", "running"):
            return detail["run"]["status"]
        await asyncio.sleep(0.1)
    return detail["run"]["status"]


async def test_cron_fires_through_the_worker_and_advances_the_schedule(client, container):
    agent_id = await _agent(client, "cron-e2e")
    trigger_id, _ = await _cron_trigger(client, agent_id)
    await _backdate(container, trigger_id)
    assert await container.scheduler.fire_due() == 1
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert await _wait_terminal(client, runs[0]["run_id"]) == "succeeded"
    assert runs[0]["metadata"]["trigger"]["trigger_id"] == trigger_id
    # the claim stamped the honest bookkeeping
    detail = (await client.get(f"/v1/triggers/{trigger_id}")).json()["trigger"]
    assert detail["last_fired_at"] is not None
    assert detail["last_error"] is None
    assert datetime.fromisoformat(detail["next_fire_at"]) > datetime.now(UTC)


async def test_catch_up_fires_exactly_once(client, container):
    agent_id = await _agent(client, "cron-catchup")
    trigger_id, _ = await _cron_trigger(client, agent_id)
    # 90 missed minutes of an every-minute schedule — the claim advances
    # next_fire_at to the NEXT slot from now, so the backlog collapses to ONE
    # fire (the scheduler is at-most-once, never a burst)
    await _backdate(container, trigger_id, minutes_ago=90)
    assert await container.scheduler.fire_due() == 1
    assert await container.scheduler.fire_due() == 0  # not due again yet
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1


async def test_disabled_fires_nothing_and_keeps_history(client, container):
    agent_id = await _agent(client, "cron-disabled")
    trigger_id, name = await _cron_trigger(client, agent_id)
    # one fire lands first
    resp = await client.post(f"/v1/triggers/{trigger_id}/fire")
    assert resp.status_code == 202, resp.text
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1
    # disable → next_fire_at clears; even a backdated row is never claimed
    patched = await client.patch(
        f"/v1/triggers/{trigger_id}",
        json={
            "name": name,
            "kind": "cron",
            "agent_id": agent_id,
            "version_policy": "latest",
            "input_template": None,
            "enabled": False,
            "config": {"type": "cron", "schedule": "* * * * *"},
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["trigger"]["next_fire_at"] is None
    await _backdate(container, trigger_id)
    assert await container.scheduler.fire_due() == 0
    assert len(await _runs_for(client, trigger_id)) == 1  # history intact


async def test_delete_keeps_run_provenance(client, container):
    agent_id = await _agent(client, "cron-deleted")
    trigger_id, _ = await _cron_trigger(client, agent_id)
    resp = await client.post(f"/v1/triggers/{trigger_id}/fire")
    assert resp.status_code == 202, resp.text
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert (await client.delete(f"/v1/triggers/{trigger_id}")).status_code == 204
    # the trigger row is gone but its runs keep the trigger_id — the
    # executions filter still finds them (they render "deleted trigger")
    assert (await client.get(f"/v1/triggers/{trigger_id}")).status_code == 404
    still = await _runs_for(client, trigger_id)
    assert [r["run_id"] for r in still] == [runs[0]["run_id"]]
    assert still[0]["metadata"]["trigger"]["trigger_id"] == trigger_id


async def test_concurrent_schedulers_never_double_fire(client, container):
    """Two fire_due cycles racing on one due trigger: the SKIP LOCKED claim
    lets exactly one win — one fire, one run."""
    agent_id = await _agent(client, "cron-race")
    trigger_id, _ = await _cron_trigger(client, agent_id)
    await _backdate(container, trigger_id)
    first, second = await asyncio.gather(
        container.scheduler.fire_due(),
        container.scheduler.fire_due(),
    )
    assert sorted((first, second)) == [0, 1]
    runs = await _wait_for_runs(client, trigger_id, 1)
    assert len(runs) == 1


async def test_firing_failure_stamps_last_error(client, container):
    """The deleted-target honesty path: a latest-policy trigger whose target
    vanished fails the fire and stamps last_error — visible, never a retry
    storm, and no run row."""
    agent_id = await _agent(client, "cron-doomed")
    trigger_id, _ = await _cron_trigger(client, agent_id)
    # the fresh agent has no executions → deletable (the 409-with-runs refusal)
    assert (await client.delete(f"/v1/agents/{agent_id}")).status_code == 204
    await _backdate(container, trigger_id)
    # claimed, then the fire failed honestly — fire_due counts SUCCESSFUL
    # fires only; the failure lives on the trigger row
    assert await container.scheduler.fire_due() == 0
    detail = (await client.get(f"/v1/triggers/{trigger_id}")).json()["trigger"]
    assert detail["last_error"] is not None
    assert "not found" in detail["last_error"]
    assert detail["last_fired_at"] is not None  # the attempt is on the record
    assert await _runs_for(client, trigger_id) == []
    # the schedule still advanced — the next slot is the retry, not a loop
    assert detail["next_fire_at"] is not None
