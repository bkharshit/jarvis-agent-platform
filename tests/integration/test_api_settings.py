"""Settings surface over HTTP (S2): member management roles, API keys
(plaintext returned exactly once), BYOK credentials (write-only, encrypted
at rest, never over the API). Security assertions are deliberately blunt:
raw response text must never contain the secret material."""

from __future__ import annotations

import base64
import json
import os

import pytest

from jarvis.security import hash_password

pytestmark = pytest.mark.db

MASTER_KEY_ENV = "JARVIS_CREDENTIALS_MASTER_KEY"
SECRET = "sk-super-secret-byok-key-material-0123456789"

CREATE_BODY = {
    "name": "settings-agent",
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "function_calling"},
}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch):
    """A deterministic master key for the suite — set before any
    encrypt/decrypt runs (the resolver loads it lazily)."""
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    monkeypatch.setenv(MASTER_KEY_ENV, key)


async def _user(container, email: str, tenant_id: str = "default", role: str = "member"):
    if tenant_id != "default":
        await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")
    return await container.auth.create_user(
        tenant_id=tenant_id, email=email, password_hash=hash_password("pw"), role=role
    )


async def _login(client, email: str, password: str = "pw") -> None:
    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


# --- members: roles and ownership guards -------------------------------------


@pytest.mark.db
async def test_member_management_requires_admin(client, container):
    member = await _user(container, "plain-member@example.com")
    await _login(client, member.email)
    assert (await client.get("/v1/members")).status_code == 403
    resp = await client.post("/v1/members", json={"email": "new@example.com", "password": "pw"})
    assert resp.status_code == 403

    owner = await _user(container, "owner@example.com", role="owner")
    await _login(client, owner.email)
    assert (await client.get("/v1/members")).status_code == 200
    created = await client.post(
        "/v1/members", json={"email": "admin-made@example.com", "role": "admin"}
    )
    assert created.status_code == 201
    assert created.json()["role"] == "admin"
    # password_hash never crosses the API
    assert "password_hash" not in created.text

    admin = await _user(container, "admin@example.com", role="admin")
    await _login(client, admin.email)
    listing = await client.get("/v1/members")
    assert listing.status_code == 200
    assert {m["email"] for m in listing.json()["items"]} >= {
        owner.email,
        admin.email,
    }


@pytest.mark.db
async def test_member_patch_delete_and_owner_protection(client, container):
    owner = await _user(container, "boss@example.com", role="owner")
    admin = await _user(container, "admin2@example.com", role="admin")
    member = await _user(container, "drone@example.com")

    await _login(client, admin.email)
    patched = await client.patch(f"/v1/members/{member.id}", json={"display_name": "Drone"})
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Drone"

    # admins cannot act on owners — no admin-driven lockout of the owner role
    assert (
        await client.patch(f"/v1/members/{owner.id}", json={"role": "member"})
    ).status_code == 403
    assert (await client.delete(f"/v1/members/{owner.id}")).status_code == 403

    # a member id from another tenant is 404, not 403 (no existence leak)
    foreign = await _user(container, "foreign@example.com", tenant_id="tenant-b")
    assert (
        await client.patch(f"/v1/members/{foreign.id}", json={"role": "member"})
    ).status_code == 404

    # delete refused while the member owns an API key — refusal covers even
    # revoked keys: the audit row survives, so the user row must too
    from jarvis.security import generate_api_key, hash_api_key, key_prefix

    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id="default",
        user_id=member.id,
        name="held",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    assert (await client.delete(f"/v1/members/{member.id}")).status_code == 409
    keys = await container.auth.list_api_keys("default")
    await container.auth.revoke_api_key(keys[0].id, "default")
    assert (await client.delete(f"/v1/members/{member.id}")).status_code == 409

    # a member with no keys or credentials deletes fine
    disposable = await _user(container, "disposable@example.com")
    assert (await client.delete(f"/v1/members/{disposable.id}")).status_code == 204


@pytest.mark.db
async def test_api_key_plaintext_returned_exactly_once(client, container):
    user = await _user(container, "key-owner@example.com")
    await _login(client, user.email)

    created = await client.post("/v1/api-keys", json={"name": "ci"})
    assert created.status_code == 201
    body = created.json()
    assert body["plaintext"].startswith("jarvis_sk_")
    assert body["key_prefix"] == body["plaintext"][:12]

    # every later read is metadata only — the plaintext is gone forever
    listing = await client.get("/v1/api-keys")
    assert listing.status_code == 200
    assert "plaintext" not in listing.text
    item = listing.json()["items"][0]
    assert item["key_prefix"] == body["key_prefix"]
    assert item["name"] == "ci"
    again = await client.get("/v1/api-keys")
    assert body["plaintext"] not in again.text

    # the anonymous principal cannot own a key (no user to attach it to)
    logout = await client.post("/v1/auth/logout")
    assert logout.status_code == 204
    assert (await client.post("/v1/api-keys", json={"name": "anon"})).status_code == 403


@pytest.mark.db
async def test_api_key_revocation_is_tenant_scoped(client, container):
    owner = await _user(container, "key-admin@example.com", role="owner")
    await _login(client, owner.email)
    created = await client.post("/v1/api-keys", json={"name": "mine"})
    key_id = created.json()["id"]

    foreign = await _user(
        container, "foreign-owner@example.com", tenant_id="tenant-b", role="owner"
    )
    await _login(client, foreign.email)
    # foreign tenant's key id → 404 (no existence leak)
    assert (await client.delete(f"/v1/api-keys/{key_id}")).status_code == 404

    await _login(client, "key-admin@example.com")
    assert (await client.delete(f"/v1/api-keys/{key_id}")).status_code == 204
    # the key no longer authenticates
    revoked = await client.get("/v1/api-keys")
    assert all(item["id"] != key_id or item["revoked_at"] for item in revoked.json()["items"])


# --- credentials: write-only, encrypted at rest, tenant-scoped ----------------


@pytest.mark.db
async def test_credential_is_write_only_and_encrypted_at_rest(client, container):
    user = await _user(container, "byok@example.com")
    await _login(client, user.email)

    created = await client.post(
        "/v1/credentials",
        json={"name": "my-openai", "provider": "openai_compatible", "secret": SECRET},
    )
    assert created.status_code == 201, created.text
    out = created.json()
    assert out["name"] == "my-openai" and out["provider"] == "openai_compatible"
    # neither the plaintext nor the envelope ever crosses the API
    assert SECRET not in created.text
    assert "secret" not in out and "ciphertext" not in out

    listing = await client.get("/v1/credentials")
    assert listing.status_code == 200
    assert SECRET not in listing.text
    assert "ciphertext" not in listing.text
    cred = listing.json()["items"][0]
    assert cred["created_by"] == user.id

    # at rest: the row carries the AES-GCM envelope, never the plaintext
    record = await container.auth.get_credential(out["id"], "default")
    assert record is not None
    assert SECRET not in json.dumps(record.ciphertext)
    assert set(record.ciphertext) == {"v", "key_id", "nonce", "ct"}

    # PATCH re-encrypts (fresh nonce/ciphertext) or renames without touching
    # the secret
    renamed = await client.patch(f"/v1/credentials/{out['id']}", json={"name": "renamed"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "renamed"
    rotated = await client.patch(f"/v1/credentials/{out['id']}", json={"secret": "sk-rotated"})
    assert rotated.status_code == 200
    assert SECRET not in rotated.text
    record2 = await container.auth.get_credential(out["id"], "default")
    assert record2 is not None and "sk-rotated" not in json.dumps(record2.ciphertext)
    assert record2.ciphertext["nonce"] != record.ciphertext["nonce"]


@pytest.mark.db
async def test_credential_ids_are_tenant_scoped_404(client, container):
    owner_a = await _user(container, "a-owner@example.com", role="owner")
    await _login(client, owner_a.email)
    created = await client.post(
        "/v1/credentials",
        json={"name": "a-cred", "provider": "openai_compatible", "secret": SECRET},
    )
    cred_id = created.json()["id"]

    owner_b = await _user(container, "b-owner@example.com", tenant_id="tenant-b", role="owner")
    await _login(client, owner_b.email)
    # foreign id → 404 on every verb (no existence leak)
    assert (await client.get("/v1/credentials")).json()["items"] == []
    assert (
        await client.patch(f"/v1/credentials/{cred_id}", json={"name": "steal"})
    ).status_code == 404
    assert (await client.delete(f"/v1/credentials/{cred_id}")).status_code == 404


@pytest.mark.db
async def test_credential_create_fails_closed_without_master_key(client, container, monkeypatch):
    user = await _user(container, "nokay@example.com")
    await _login(client, user.email)
    monkeypatch.delenv(MASTER_KEY_ENV)
    resp = await client.post(
        "/v1/credentials",
        json={"name": "x", "provider": "openai_compatible", "secret": "v"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["kind"] == "credentials_unavailable"
    # nothing was written
    assert (await client.get("/v1/credentials")).json()["items"] == []


@pytest.mark.db
async def test_agent_snapshot_never_holds_credential_material(client, container, agent):
    owner = await _user(container, "snap@example.com", role="owner")
    await _login(client, owner.email)
    created = await client.post(
        "/v1/credentials",
        json={"name": "snap-cred", "provider": "openai_compatible", "secret": SECRET},
    )
    cred_id = created.json()["id"]

    # bind the agent's model to the stored credential reference (S2 model UI)
    from jarvis.domain.agent import StoredCredentialRef

    updated = await client.patch(
        f"/v1/agents/{agent.id}",
        json={
            "model": {
                "provider": "openai_compatible",
                "model": "m",
                "credential_ref": {
                    "type": "stored",
                    "credential_id": cred_id,
                },
            }
        },
    )
    assert updated.status_code == 200, updated.text

    # API snapshot: the reference is there, the material is not
    detail = await client.get(f"/v1/agents/{agent.id}")
    assert detail.status_code == 200
    assert cred_id in detail.text
    assert SECRET not in detail.text

    # persisted snapshot row: same assertion straight from the repo
    version = await container.agents.latest_version(agent.id)
    assert version is not None
    assert SECRET not in json.dumps(version.snapshot.model_dump(), default=str)
    assert version.snapshot.model.credential_ref == StoredCredentialRef(
        type="stored", credential_id=cred_id
    )
