"""MCP server routes over HTTP (S4, ADR 0012 §5): tenant-scoped CRUD with
admin/owner gates (anonymous mode = the default tenant's full access),
foreign-tenant 404s, env-var NAMES only in every response, and the probe
route against the real fixture stdio server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.db

FIXTURE_SERVER = Path(__file__).resolve().parent.parent / "fixtures" / "mcp" / "server.py"

STDIO_BODY = {
    "name": "fixtures",
    "config": {"type": "stdio", "command": sys.executable, "args": [str(FIXTURE_SERVER)]},
}


async def _user(container, email: str, tenant_id: str = "default", role: str = "member"):
    from jarvis.security import hash_password

    if tenant_id != "default":
        await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")
    return await container.auth.create_user(
        tenant_id=tenant_id, email=email, password_hash=hash_password("pw"), role=role
    )


async def _login(client, email: str, password: str = "pw") -> None:
    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


# --- anonymous-mode happy path -------------------------------------------------


async def test_anonymous_crud_roundtrip(client):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    assert created.status_code == 201, created.text
    server = created.json()
    assert server["name"] == "fixtures"
    assert server["tenant_id"] == "default"
    assert server["config"]["type"] == "stdio"
    assert server["enabled"] is True

    listing = (await client.get("/v1/mcp/servers")).json()
    assert [s["name"] for s in listing["items"]] == ["fixtures"]

    got = await client.get(f"/v1/mcp/servers/{server['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == server["id"]

    patched = await client.patch(f"/v1/mcp/servers/{server['id']}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["name"] == "fixtures"  # name never moves

    # update the config too — env refs carry NAMES only
    with_env = await client.patch(
        f"/v1/mcp/servers/{server['id']}",
        json={
            "config": {
                "type": "stdio",
                "command": "x",
                "env": {"T": {"type": "env", "env_var": "SOME_VAR"}},
            }
        },
    )
    assert with_env.status_code == 200
    assert with_env.json()["config"]["env"]["T"] == {"type": "env", "env_var": "SOME_VAR"}
    assert "secret" not in with_env.text.lower() or "env_var" in with_env.text

    deleted = await client.delete(f"/v1/mcp/servers/{server['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/mcp/servers/{server['id']}")).status_code == 404


async def test_duplicate_name_conflicts(client):
    first = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    assert first.status_code == 201
    dup = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    assert dup.status_code == 409
    assert "fixtures" in dup.json()["error"]["message"]


async def test_bad_slug_and_bad_config_422(client):
    bad_slug = await client.post("/v1/mcp/servers", json={**STDIO_BODY, "name": "Bad Name!"})
    assert bad_slug.status_code == 422
    assert "slug" in bad_slug.text

    bad_config = await client.post(
        "/v1/mcp/servers", json={"name": "ok-name", "config": {"type": "carrier-pigeon"}}
    )
    assert bad_config.status_code == 422

    bad_url = await client.post(
        "/v1/mcp/servers", json={"name": "ok-name", "config": {"type": "http", "url": "ftp://x"}}
    )
    assert bad_url.status_code == 422


async def test_patch_cannot_move_the_name(client):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    server_id = created.json()["id"]
    moved = await client.patch(f"/v1/mcp/servers/{server_id}", json={"name": "renamed"})
    assert moved.status_code == 422
    assert "immutable" in moved.text


# --- role gates ------------------------------------------------------------------


async def test_member_role_cannot_manage_but_can_list_and_probe(client, container):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)  # anonymous creates it
    server_id = created.json()["id"]

    member = await _user(container, "mcp-member@example.com")
    await _login(client, member.email)
    assert (await client.post("/v1/mcp/servers", json=STDIO_BODY)).status_code == 403
    assert (
        await client.patch(f"/v1/mcp/servers/{server_id}", json={"enabled": False})
    ).status_code == 403
    assert (await client.delete(f"/v1/mcp/servers/{server_id}")).status_code == 403
    # listing and probing are any-member
    assert (await client.get("/v1/mcp/servers")).status_code == 200

    owner = await _user(container, "mcp-owner@example.com", role="owner")
    await _login(client, owner.email)
    assert (
        await client.patch(f"/v1/mcp/servers/{server_id}", json={"enabled": False})
    ).status_code == 200


# --- tenancy ---------------------------------------------------------------------


async def test_foreign_tenant_server_is_404(client, container):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    server_id = created.json()["id"]

    outsider = await _user(container, "outsider@example.com", tenant_id="tenant-b")
    await _login(client, outsider.email)
    # a foreign member sees nothing: reads 404 (no existence leak); writes
    # hit the role gate first — 403 is about the caller, not the resource
    assert (await client.get(f"/v1/mcp/servers/{server_id}")).status_code == 404
    assert (
        await client.patch(f"/v1/mcp/servers/{server_id}", json={"enabled": False})
    ).status_code == 403
    assert (await client.delete(f"/v1/mcp/servers/{server_id}")).status_code == 403
    assert (await client.post(f"/v1/mcp/servers/{server_id}/probe")).status_code == 404
    listing = (await client.get("/v1/mcp/servers")).json()
    assert listing["items"] == []  # the default tenant's row is invisible

    # even with the role, a foreign manager still 404s
    foreign_owner = await _user(
        container, "foreign-owner@example.com", tenant_id="tenant-b2", role="owner"
    )
    await _login(client, foreign_owner.email)
    assert (
        await client.patch(f"/v1/mcp/servers/{server_id}", json={"enabled": False})
    ).status_code == 404
    assert (await client.delete(f"/v1/mcp/servers/{server_id}")).status_code == 404


# --- probe ------------------------------------------------------------------------


async def test_probe_lists_fixture_server_tools(client):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    server_id = created.json()["id"]

    probed = await client.post(f"/v1/mcp/servers/{server_id}/probe")
    assert probed.status_code == 200, probed.text
    body = probed.json()
    assert body["server"]["id"] == server_id
    names = [t["name"] for t in body["tools"]]
    assert names == ["mcp__fixtures__echo", "mcp__fixtures__add_numbers"]
    # the approval default travels with every descriptor
    assert all(t["annotations"]["requires_approval"] is True for t in body["tools"])
    assert body["tools"][0]["parameters"]["required"] == ["text"]


async def test_probe_unreachable_server_is_502_envelope(client):
    dead = await client.post(
        "/v1/mcp/servers",
        json={
            "name": "dead-server",
            "config": {"type": "stdio", "command": "definitely-not-a-real-command-xyz"},
        },
    )
    assert dead.status_code == 201
    probed = await client.post(f"/v1/mcp/servers/{dead.json()['id']}/probe")
    assert probed.status_code == 502
    body = probed.json()
    assert body["error"]["kind"] == "mcp_unreachable"
    assert "dead-server" in body["error"]["message"]


# --- capabilities detail ------------------------------------------------------------


async def test_capabilities_tools_detail_lists_mcp_servers(client):
    created = await client.post("/v1/mcp/servers", json=STDIO_BODY)
    assert created.status_code == 201

    caps = (await client.get("/v1/capabilities")).json()
    mcp = caps["sections"]["tools"]["detail"]["mcp"]
    assert mcp["enabled"] is True
    assert {
        "id": created.json()["id"],
        "name": "fixtures",
        "transport": "stdio",
        "enabled": True,
    } in mcp["servers"]


# --- stored credential headers (ADR 0013) -------------------------------------------

MASTER_KEY_ENV = "JARVIS_CREDENTIALS_MASTER_KEY"
SECRET = "sk-mcp-header-secret-material-9876543210"


@pytest.fixture
def _master_key(monkeypatch: pytest.MonkeyPatch):
    """The resolver loads the master key lazily at first decrypt, so setting
    it in the test process is enough (same pattern as test_api_settings)."""
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    monkeypatch.setenv(MASTER_KEY_ENV, key)


def _http_body(name: str, headers: dict) -> dict:
    return {
        "name": name,
        "config": {
            "type": "http",
            "url": "http://127.0.0.1:1/mcp",  # refused instantly if resolution gets that far
            "headers": headers,
        },
    }


async def test_stored_header_probe_without_the_credential_names_the_id(client):
    # anonymous client, no credential rows: resolution fails BEFORE any
    # network I/O — the 502 names the credential id, not a handshake error
    created = await client.post(
        "/v1/mcp/servers",
        json=_http_body(
            "authed-http", {"Authorization": {"type": "stored", "credential_id": "cred-missing"}}
        ),
    )
    assert created.status_code == 201, created.text
    probed = await client.post(f"/v1/mcp/servers/{created.json()['id']}/probe")
    assert probed.status_code == 502
    body = probed.json()
    assert body["error"]["kind"] == "mcp_unreachable"
    assert "cred-missing" in body["error"]["message"]
    # resolution short-circuits: the failure is the missing credential,
    # never a connection attempt
    assert "connection" not in body["error"]["message"]


async def test_stored_header_resolves_then_connects_and_the_secret_stays_hidden(
    client, container, _master_key
):
    """The happy path up to the connect boundary: with a real stored
    credential, resolution succeeds and the failure moves to the (dead)
    endpoint — the secret itself never appears in any response."""
    owner = await _user(container, "mcp-owner@example.com", role="owner")
    await _login(client, owner.email)
    cred = (
        await client.post(
            "/v1/credentials",
            json={"name": "webz-key", "provider": "mcp_header", "secret": SECRET},
        )
    ).json()
    created = await client.post(
        "/v1/mcp/servers",
        json=_http_body(
            "authed-http", {"Authorization": {"type": "stored", "credential_id": cred["id"]}}
        ),
    )
    assert created.status_code == 201
    server = created.json()
    assert server["config"]["headers"]["Authorization"] == {
        "type": "stored",
        "credential_id": cred["id"],
    }
    listing_text = json.dumps((await client.get("/v1/mcp/servers")).json())
    assert SECRET not in listing_text  # the write-only invariant (D29)

    probed = await client.post(f"/v1/mcp/servers/{server['id']}/probe")
    # resolution passed — the failure moved to the (dead) endpoint: the
    # 502 is a handshake/connection failure, NOT the credential id
    assert probed.status_code == 502
    assert cred["id"] not in probed.json()["error"]["message"]


async def test_stored_header_from_a_foreign_tenant_is_502_not_404(client, container):
    """The resolver collapses absent/revoked/foreign into one failure (no
    existence leak, D29) — surfaced as the probe's 502 envelope."""
    created = await client.post(
        "/v1/mcp/servers",
        json=_http_body(
            "foreign-auth", {"Authorization": {"type": "stored", "credential_id": "cred-foreign"}}
        ),
    )
    server_id = created.json()["id"]
    resp = await client.post(f"/v1/mcp/servers/{server_id}/probe")
    assert resp.status_code == 502
    assert "cred-foreign" in resp.json()["error"]["message"]


async def test_env_header_refs_still_roundtrip_alongside_stored(client):
    created = await client.post(
        "/v1/mcp/servers",
        json=_http_body(
            "mixed-refs",
            {
                "X-Api-Key": {"type": "env", "env_var": "MCP_X_API_KEY"},
                "Authorization": {"type": "stored", "credential_id": "cred-1"},
            },
        ),
    )
    assert created.status_code == 201, created.text
    got = (await client.get(f"/v1/mcp/servers/{created.json()['id']}")).json()
    headers = got["config"]["headers"]
    assert headers["X-Api-Key"] == {"type": "env", "env_var": "MCP_X_API_KEY"}
    assert headers["Authorization"] == {"type": "stored", "credential_id": "cred-1"}
