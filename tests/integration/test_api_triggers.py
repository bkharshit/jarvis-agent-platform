"""Triggers CRUD over HTTP (S13, ADR 0019 §6, D62): the MCP route pattern —
secret shown exactly once, cron parse 422s, pin stamping + stale-pin lint,
fire-now 409-when-disabled, and the executions trigger_id filter."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jarvis.persistence.repositories import SqlTriggerRepo

pytestmark = pytest.mark.db

MASTER_KEY_ENV = "JARVIS_CREDENTIALS_MASTER_KEY"

AGENT_BODY = {
    "name": "watched-agent",
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


async def _agent(client) -> str:
    resp = await client.post("/v1/agents", json=AGENT_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()["definition"]["id"]


def _cron_body(agent_id: str, **overrides) -> dict:
    body: dict = {
        "name": "nightly",
        "kind": "cron",
        "agent_id": agent_id,
        "config": {"type": "cron", "schedule": "0 3 * * *"},
    }
    body.update(overrides)
    return body


async def test_create_cron_stamps_next_fire_and_hides_secret(client):
    agent_id = await _agent(client)
    resp = await client.post("/v1/triggers", json=_cron_body(agent_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["trigger"]["kind"] == "cron"
    assert body["trigger"]["next_fire_at"] is not None
    assert body["trigger"]["agent_version_id"] is None  # latest policy: no pin
    assert "secret" not in body["trigger"]


async def test_bad_cron_expression_422s(client):
    agent_id = await _agent(client)
    resp = await client.post(
        "/v1/triggers", json=_cron_body(agent_id, config={"type": "cron", "schedule": "not a cron"})
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["kind"] == "validation"


async def test_kind_config_mismatch_422s(client):
    agent_id = await _agent(client)
    resp = await client.post("/v1/triggers", json=_cron_body(agent_id, config={"type": "webhook"}))
    assert resp.status_code == 422
    assert "must agree" in resp.json()["error"]["message"]


async def test_target_without_published_version_422s(client):
    resp = await client.post("/v1/triggers", json=_cron_body("agent-that-does-not-exist"))
    assert resp.status_code == 422
    assert "no published version" in resp.json()["error"]["message"]


async def test_pinned_policy_stamps_and_lints_after_republish(client):
    agent_id = await _agent(client)
    created = await client.post(
        "/v1/triggers",
        json=_cron_body(agent_id, version_policy="pinned"),
    )
    trigger_id = created.json()["trigger"]["id"]
    assert created.json()["trigger"]["agent_version_id"] is not None
    detail = await client.get(f"/v1/triggers/{trigger_id}")
    assert detail.json()["lints"] == []
    # republish → the pin goes stale
    await client.patch(f"/v1/agents/{agent_id}", json={"description": "v2"})
    detail = (await client.get(f"/v1/triggers/{trigger_id}")).json()
    assert detail["lints"] == ["target republished since pin"]


async def test_memory_on_target_lints(client):
    agent_id = await _agent(client)
    # memory-enabled target: trigger-fired runs are memory-less (S14 child
    # precedent) — the detail says so honestly
    await client.patch(
        f"/v1/agents/{agent_id}", json={"memory": {"enabled": True, "max_messages": 5}}
    )
    created = await client.post("/v1/triggers", json=_cron_body(agent_id))
    detail = (await client.get(f"/v1/triggers/{created.json()['trigger']['id']}")).json()
    assert detail["lints"] == ["memory enabled on target; trigger-fired runs are memory-less"]


async def test_duplicate_name_conflicts(client):
    agent_id = await _agent(client)
    await client.post("/v1/triggers", json=_cron_body(agent_id))
    resp = await client.post("/v1/triggers", json=_cron_body(agent_id))
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "conflict"


async def test_patch_is_full_document_and_recomputes_next_fire(client):
    agent_id = await _agent(client)
    trigger_id = (await client.post("/v1/triggers", json=_cron_body(agent_id))).json()["trigger"][
        "id"
    ]
    resp = await client.patch(
        f"/v1/triggers/{trigger_id}",
        json={
            "name": "renamed",
            "kind": "cron",
            "agent_id": agent_id,
            "version_policy": "latest",
            "input_template": None,
            "enabled": False,
            "config": {"type": "cron", "schedule": "*/5 * * * *"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trigger"]["name"] == "renamed"
    assert body["trigger"]["next_fire_at"] is None  # disabled fires nothing
    # re-enable → a fresh due time from now
    resp = await client.patch(
        f"/v1/triggers/{trigger_id}",
        json={
            "name": "renamed",
            "kind": "cron",
            "agent_id": agent_id,
            "version_policy": "latest",
            "input_template": None,
            "enabled": True,
            "config": {"type": "cron", "schedule": "*/5 * * * *"},
        },
    )
    assert resp.json()["trigger"]["next_fire_at"] is not None


async def test_delete_is_always_204(client):
    agent_id = await _agent(client)
    trigger_id = (await client.post("/v1/triggers", json=_cron_body(agent_id))).json()["trigger"][
        "id"
    ]
    assert (await client.delete(f"/v1/triggers/{trigger_id}")).status_code == 204
    assert (await client.get(f"/v1/triggers/{trigger_id}")).status_code == 404


# --- webhook secrets -------------------------------------------------------------


@pytest.fixture
def _master_key(monkeypatch: pytest.MonkeyPatch):
    """The webhook seal loads the master key per request — setting the env
    var in the test process is enough (the test_api_mcp pattern)."""
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    monkeypatch.setenv(MASTER_KEY_ENV, key)


async def test_webhook_secret_shown_exactly_once_and_never_in_gets(client, _master_key):
    agent_id = await _agent(client)
    created = await client.post(
        "/v1/triggers",
        json={
            "name": "hook",
            "kind": "webhook",
            "agent_id": agent_id,
            "config": {"type": "webhook"},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert isinstance(body["secret"], str) and len(body["secret"]) > 20
    assert "secret" not in body["trigger"]
    detail = (await client.get(f"/v1/triggers/{body['trigger']['id']}")).json()
    assert "secret" not in detail["trigger"]
    listed = (await client.get("/v1/triggers")).json()
    assert all("secret" not in t for t in listed["items"])


async def test_rotate_secret_returns_a_new_plaintext(client, _master_key):
    agent_id = await _agent(client)
    trigger_id = (
        await client.post(
            "/v1/triggers",
            json={
                "name": "hook",
                "kind": "webhook",
                "agent_id": agent_id,
                "config": {"type": "webhook"},
            },
        )
    ).json()["trigger"]["id"]
    first = (await client.post(f"/v1/triggers/{trigger_id}/rotate-secret")).json()["secret"]
    second = (await client.post(f"/v1/triggers/{trigger_id}/rotate-secret")).json()["secret"]
    assert first != second


async def test_rotate_secret_on_cron_422s(client):
    agent_id = await _agent(client)
    trigger_id = (await client.post("/v1/triggers", json=_cron_body(agent_id))).json()["trigger"][
        "id"
    ]
    resp = await client.post(f"/v1/triggers/{trigger_id}/rotate-secret")
    assert resp.status_code == 422


# --- fire-now + the executions filter ----------------------------------------------


async def test_fire_now_is_409_when_disabled(client):
    agent_id = await _agent(client)
    trigger_id = (
        await client.post("/v1/triggers", json=_cron_body(agent_id, enabled=False))
    ).json()["trigger"]["id"]
    resp = await client.post(f"/v1/triggers/{trigger_id}/fire")
    assert resp.status_code == 409
    assert "disabled" in resp.json()["error"]["message"]


async def test_fire_now_enqueues_an_ordinary_run_and_the_filter_finds_it(client, container):
    agent_id = await _agent(client)
    trigger_id = (await client.post("/v1/triggers", json=_cron_body(agent_id))).json()["trigger"][
        "id"
    ]
    resp = await client.post(f"/v1/triggers/{trigger_id}/fire")
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    # The fired run is an ordinary queue run — provenance filterable, and it
    # executes end-to-end on the embedded worker (the mock provider).
    for _ in range(50):
        runs = (await client.get("/v1/executions", params={"trigger_id": trigger_id})).json()
        if any(r["run_id"] == run_id for r in runs["items"]):
            break
        await asyncio.sleep(0.1)
    assert any(r["run_id"] == run_id for r in runs["items"])
    # the provenance rides metadata["trigger"] (D62); the column is the filter
    assert all(r["metadata"]["trigger"]["trigger_id"] == trigger_id for r in runs["items"])


async def test_executions_filter_is_empty_for_unfired_triggers(client, container):
    agent_id = await _agent(client)
    trigger_id = (await client.post("/v1/triggers", json=_cron_body(agent_id))).json()["trigger"][
        "id"
    ]
    runs = (await client.get("/v1/executions", params={"trigger_id": trigger_id})).json()
    assert runs["items"] == []


# --- capabilities (the S4 derived-facts gotcha: same commit as the table) --------


async def test_capabilities_reports_triggers_live_with_counts(client, _master_key):
    agent_id = await _agent(client)
    await client.post("/v1/triggers", json=_cron_body(agent_id))
    await client.post(
        "/v1/triggers",
        json={
            "name": "hook",
            "kind": "webhook",
            "agent_id": agent_id,
            "config": {"type": "webhook"},
        },
    )
    caps = (await client.get("/v1/capabilities")).json()
    section = caps["sections"]["triggers"]
    assert section["enabled"] is True
    assert section["detail"] == {"total": 2, "cron": 1, "webhook": 1, "event": 0}


async def test_trigger_rows_survive_the_api_roundtrip_through_the_repo(client, container):
    """The repo layer agrees with the API layer: a created trigger reads
    back through SqlTriggerRepo with the stamped pin and due time."""
    agent_id = await _agent(client)
    body = (
        await client.post("/v1/triggers", json=_cron_body(agent_id, version_policy="pinned"))
    ).json()["trigger"]
    repo = SqlTriggerRepo(async_sessionmaker(container.engine, expire_on_commit=False))
    loaded = await repo.get(body["id"], tenant_id="default")
    assert loaded is not None
    assert loaded.agent_version_id == body["agent_version_id"]
    assert loaded.next_fire_at is not None
