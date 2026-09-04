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
