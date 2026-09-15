"""Agents CRUD over HTTP: auto-versioning, conflict semantics, envelopes."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db

CREATE_BODY = {
    "name": "crud-agent",
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
    "description": "first",
}


@pytest.mark.db
async def test_create_publishes_version_one(client):
    resp = await client.post("/v1/agents", json=CREATE_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["definition"]["name"] == "crud-agent"
    assert [v["version"] for v in body["versions"]] == [1]
    assert body["versions"][0]["label"] == "initial"


@pytest.mark.db
async def test_duplicate_name_conflicts(client):
    await client.post("/v1/agents", json=CREATE_BODY)
    resp = await client.post("/v1/agents", json=CREATE_BODY)
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "conflict"


@pytest.mark.db
async def test_patch_auto_publishes_new_version(client):
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    resp = await client.patch(f"/v1/agents/{agent_id}", json={"description": "second"})
    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()["versions"]] == [1, 2]

    frozen = await client.get(f"/v1/agents/{agent_id}/versions/1")
    assert frozen.status_code == 200
    assert frozen.json()["snapshot"]["description"] == "first"  # history intact
    current = await client.get(f"/v1/agents/{agent_id}")
    assert current.json()["definition"]["description"] == "second"


@pytest.mark.db
async def test_get_unknown_agent_404_envelope(client):
    resp = await client.get("/v1/agents/nope")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["kind"] == "not_found" and "message" in error


@pytest.mark.db
async def test_validation_error_envelope(client):
    resp = await client.post("/v1/agents", json={"name": "x"})  # missing model+strategy
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["kind"] == "validation"
    assert error["details"]["errors"]


@pytest.mark.db
async def test_create_rejects_unknown_strategy_type(client):
    # D36: the domain type is a free string; the create boundary 422s with
    # the live registry's known list.
    resp = await client.post(
        "/v1/agents", json={**CREATE_BODY, "strategy": {"type": "plan_execute"}}
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["kind"] == "validation"
    assert "plan_execute" in error["message"]
    assert "function_calling" in error["message"]  # the known list is named
    assert error["details"]["errors"][0]["loc"] == ["body", "strategy", "type"]


@pytest.mark.db
async def test_patch_rejects_unknown_strategy_but_keeps_agent_editable(client):
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    resp = await client.patch(
        f"/v1/agents/{agent_id}", json={"strategy": {"type": "tree_of_thought"}}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["kind"] == "validation"
    # a payload that doesn't touch strategy is unaffected
    resp = await client.patch(f"/v1/agents/{agent_id}", json={"description": "edited"})
    assert resp.status_code == 200


@pytest.mark.db
async def test_delete_refused_with_executions(client, agent, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("done"))
    resp = await client.post(f"/v1/agents/{agent.id}/run", json={"input": "go"})
    assert resp.status_code == 200

    resp = await client.delete(f"/v1/agents/{agent.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "conflict"


@pytest.mark.db
async def test_delete_without_executions(client):
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    resp = await client.delete(f"/v1/agents/{agent_id}")
    assert resp.status_code == 204
    assert (await client.get(f"/v1/agents/{agent_id}")).status_code == 404


# --- S14 (ADR 0018 §1): sub-agent publish pinning -------------------------------


async def _create_worker(client, **overrides) -> tuple[str, str]:
    body = {
        "name": f"worker-{overrides.get('name', 'a')}",
        "model": {"provider": "mock", "model": "mock-model"},
        "strategy": {"type": "function_calling"},
        **overrides,
    }
    resp = await client.post("/v1/agents", json=body)
    assert resp.status_code == 201
    detail = resp.json()
    return detail["definition"]["id"], detail["versions"][0]["id"]


def _supervisor_body(worker_id: str, pin: str | None = "client-garbage") -> dict:
    config = {"agent_id": worker_id}
    if pin is not None:
        config["agent_version_id"] = pin
    return {
        "name": "supervisor",
        "model": {"provider": "mock", "model": "mock-model"},
        "strategy": {"type": "function_calling"},
        "tools": [{"name": "sub__worker", "config": config}],
    }


@pytest.mark.db
async def test_publish_overwrites_client_pins_with_server_stamps(client):
    worker_id, worker_version = await _create_worker(client)
    resp = await client.post("/v1/agents", json=_supervisor_body(worker_id))
    assert resp.status_code == 201
    binding = resp.json()["definition"]["tools"][0]
    # D51: the client's garbage pin was overwritten, never trusted
    assert binding["config"]["agent_version_id"] == worker_version
    assert binding["config"]["agent_id"] == worker_id


@pytest.mark.db
async def test_binding_without_agent_id_422s(client):
    resp = await client.post("/v1/agents", json=_supervisor_body("whatever", pin=None))
    assert resp.status_code == 422
    assert "sub__worker" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_binding_to_unpublished_or_foreign_agent_422s(client):
    resp = await client.post("/v1/agents", json=_supervisor_body("no-such-agent"))
    assert resp.status_code == 422
    assert "no-such-agent" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_self_binding_422s(client):
    # a create-time binding cannot know the server-generated id — the
    # direct self-binding is a PATCH: agent X binds agent X
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    resp = await client.patch(
        f"/v1/agents/{agent_id}",
        json={"tools": [{"name": "sub__crud-agent", "config": {"agent_id": agent_id}}]},
    )
    assert resp.status_code == 422
    assert "itself" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_cycle_422s_at_publish(client):
    # A publishes binding B; then A republishes binding B while B binds back
    # to A — the A→B→A cycle is refused at A's republish
    agent_a, _ = await _create_worker(client, name="cycle-a")
    body_b = {
        "name": "cycle-b",
        "model": {"provider": "mock", "model": "mock-model"},
        "strategy": {"type": "function_calling"},
        "tools": [{"name": "sub__worker-cycle-a", "config": {"agent_id": agent_a}}],
    }
    resp = await client.post("/v1/agents", json=body_b)
    assert resp.status_code == 201
    agent_b = resp.json()["definition"]["id"]
    cycle_body = {
        "name": "cycle-a",
        "model": {"provider": "mock", "model": "mock-model"},
        "strategy": {"type": "function_calling"},
        "tools": [{"name": "sub__cycle-b", "config": {"agent_id": agent_b}}],
    }
    resp = await client.patch(f"/v1/agents/{agent_a}", json=cycle_body)
    assert resp.status_code == 422
    assert "cycle" in resp.json()["error"]["message"]


@pytest.mark.db
async def test_stale_pin_lint_after_target_republish(client):
    worker_id, _ = await _create_worker(client)
    sup_id = (await client.post("/v1/agents", json=_supervisor_body(worker_id))).json()[
        "definition"
    ]["id"]
    detail = (await client.get(f"/v1/agents/{sup_id}")).json()
    assert detail["lints"] == []
    # republish the worker — the supervisor's pin is now stale (a warning,
    # never a failure)
    await client.patch(f"/v1/agents/{worker_id}", json={"description": "v2"})
    detail = (await client.get(f"/v1/agents/{sup_id}")).json()
    assert len(detail["lints"]) == 1
    assert "stale" in detail["lints"][0]


@pytest.mark.db
async def test_memory_on_child_lint(client):
    worker_id, worker_version = await _create_worker(client, memory={"enabled": True})
    sup_id = (await client.post("/v1/agents", json=_supervisor_body(worker_id))).json()[
        "definition"
    ]["id"]
    detail = (await client.get(f"/v1/agents/{sup_id}")).json()
    assert len(detail["lints"]) == 1
    assert "memory" in detail["lints"][0]
