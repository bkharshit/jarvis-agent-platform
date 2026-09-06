"""Auth surface over HTTP (S2): login/logout/whoami, API-key Bearer auth,
required mode, and tenant scoping of the agents/executions/conversations
routes (foreign tenant → 404, no existence leak)."""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.security import generate_api_key, hash_api_key, key_prefix

pytestmark = pytest.mark.db

CREATE_BODY = {
    "name": "scoped-agent",
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


async def _user(container, email: str, tenant_id: str = "default", role: str = "member"):
    from jarvis.security import hash_password

    if tenant_id != "default":
        await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")
    return await container.auth.create_user(
        tenant_id=tenant_id, email=email, password_hash=hash_password("pw"), role=role
    )


async def _api_key_for(container, user, tenant_id: str = "default") -> str:
    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id=tenant_id,
        user_id=user.id,
        name="test",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    return plaintext


@pytest.mark.db
async def test_anonymous_whoami_is_the_default_tenant(client):
    resp = await client.get("/v1/auth/whoami")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "anonymous"
    assert body["tenant_id"] == "default"
    assert body["email"] is None and body["user_id"] is None


@pytest.mark.db
async def test_login_sets_cookie_and_whoami_reports_session(client, container):
    user = await _user(container, "login@example.com")
    resp = await client.post(
        "/v1/auth/login", json={"email": "login@example.com", "password": "pw"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "session" and body["email"] == "login@example.com"
    assert body["role"] == "member"
    assert "jarvis_session" in resp.cookies

    whoami = await client.get("/v1/auth/whoami")
    assert whoami.status_code == 200
    assert whoami.json()["mode"] == "session"
    assert whoami.json()["user_id"] == user.id


@pytest.mark.db
async def test_login_rejects_bad_credentials(client, container):
    await _user(container, "bad@example.com")
    for body in (
        {"email": "nobody@example.com", "password": "pw"},  # unknown email
        {"email": "bad@example.com", "password": "wrong"},  # wrong password
    ):
        resp = await client.post("/v1/auth/login", json=body)
        assert resp.status_code == 401
        assert resp.json()["error"]["kind"] == "unauthenticated"


@pytest.mark.db
async def test_logout_destroys_the_session(client, container):
    await _user(container, "out@example.com")
    await client.post("/v1/auth/login", json={"email": "out@example.com", "password": "pw"})
    assert (await client.get("/v1/auth/whoami")).json()["mode"] == "session"

    logout = await client.post("/v1/auth/logout")
    assert logout.status_code == 204
    resp = await client.get("/v1/auth/whoami")
    assert resp.json()["mode"] == "anonymous"  # cookie cleared → anonymous fallback


@pytest.mark.db
async def test_api_key_bearer_auth(client, container):
    user = await _user(container, "keyer@example.com")
    plaintext = await _api_key_for(container, user)
    resp = await client.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "api_key"
    assert body["email"] == "keyer@example.com"
    assert body["user_id"] == user.id


@pytest.mark.db
async def test_presented_but_invalid_credentials_are_always_401(client, container):
    # a bad bearer is rejected even in anonymous mode — falling back would
    # mask revocation
    bad_bearer = await client.get(
        "/v1/auth/whoami", headers={"Authorization": f"Bearer {generate_api_key()}"}
    )
    assert bad_bearer.status_code == 401
    non_key = await client.get(
        "/v1/auth/whoami", headers={"Authorization": "Bearer sk-foreign-key"}
    )
    assert non_key.status_code == 401
    # an expired/unknown session cookie likewise
    client.cookies.set("jarvis_session", "0" * 64)
    stale = await client.get("/v1/auth/whoami")
    assert stale.status_code == 401


@pytest.mark.db
async def test_required_mode_rejects_unauthenticated(client, container):
    container.settings = Settings(
        auth_mode="required", database_url=container.settings.database_url
    )
    resp = await client.get("/v1/auth/whoami")
    assert resp.status_code == 401
    assert resp.json()["error"]["kind"] == "unauthenticated"

    # and a valid API key gets through
    user = await _user(container, "req@example.com")
    plaintext = await _api_key_for(container, user)
    ok = await client.get("/v1/auth/whoami", headers={"Authorization": f"Bearer {plaintext}"})
    assert ok.status_code == 200 and ok.json()["mode"] == "api_key"


@pytest.mark.db
async def test_agents_are_tenant_scoped_across_the_api(client, container):
    """An agent created in tenant A is invisible to tenant B — 404, and the
    list never leaks it."""
    created = await client.post("/v1/agents", json=CREATE_BODY)  # anonymous → default
    agent_id = created.json()["definition"]["id"]

    other = await _user(container, "other@example.com", tenant_id="tenant-b")
    other_key = await _api_key_for(container, other, tenant_id="tenant-b")
    headers = {"Authorization": f"Bearer {other_key}"}

    assert (await client.get(f"/v1/agents/{agent_id}", headers=headers)).status_code == 404
    listed = (await client.get("/v1/agents", headers=headers)).json()
    assert [a["id"] for a in listed["items"]] == []

    # tenant-b can still create and see its own agent
    own = await client.post(
        "/v1/agents", json={**CREATE_BODY, "name": "own-agent"}, headers=headers
    )
    assert own.status_code == 201
    mine = (await client.get("/v1/agents", headers=headers)).json()
    assert [a["name"] for a in mine["items"]] == ["own-agent"]

    # the default tenant still sees its own agent but not tenant-b's
    default_list = (await client.get("/v1/agents")).json()
    assert {a["name"] for a in default_list["items"]} == {"scoped-agent"}


@pytest.mark.db
async def test_run_and_execution_are_tenant_scoped(client, container, mock):
    from jarvis.models.mock import turn

    mock.add_turn(turn("done"))
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    run_resp = await client.post(f"/v1/agents/{agent_id}/run", json={"input": "hi"})
    assert run_resp.status_code == 200
    run_id = run_resp.json()["run_id"]

    other = await _user(container, "peek@example.com", tenant_id="tenant-b")
    other_key = await _api_key_for(container, other, tenant_id="tenant-b")
    headers = {"Authorization": f"Bearer {other_key}"}

    assert (await client.get(f"/v1/executions/{run_id}", headers=headers)).status_code == 404
    assert (await client.get(f"/v1/executions/{run_id}/events", headers=headers)).status_code == 404
    listed = (await client.get("/v1/executions", headers=headers)).json()
    assert listed["items"] == []

    # the owning (anonymous/default) principal still sees everything
    assert (await client.get(f"/v1/executions/{run_id}")).status_code == 200


@pytest.mark.db
async def test_conversations_are_tenant_scoped(client, container):
    agent_id = (await client.post("/v1/agents", json=CREATE_BODY)).json()["definition"]["id"]
    resp = await client.get(f"/v1/conversations/{agent_id}/sess-1/messages")
    assert resp.status_code == 404  # no conversation yet — but auth passes

    other = await _user(container, "conv@example.com", tenant_id="tenant-b")
    other_key = await _api_key_for(container, other, tenant_id="tenant-b")
    headers = {"Authorization": f"Bearer {other_key}"}
    assert (
        await client.get(f"/v1/conversations/{agent_id}/sess-1/messages", headers=headers)
    ).status_code == 404
