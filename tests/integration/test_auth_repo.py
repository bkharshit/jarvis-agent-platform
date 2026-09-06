"""SqlAuthRepo integration: users/sessions/api-keys/credentials — tenant
scoping (foreign ids resolve to None/False), hash-only lookups, expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from jarvis.domain.auth import ApiKeyRecord, StoredCredentialRecord, UserAccount
from jarvis.persistence.repositories import SqlAuthRepo
from jarvis.security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    key_prefix,
)

pytestmark = pytest.mark.db

TENANT = "default"  # seeded by migration 0004 / conftest truncate
OTHER_TENANT = "tenant-b"


async def _make_tenant(repo: SqlAuthRepo, tenant_id: str) -> None:
    await repo.create_tenant(tenant_id, f"Tenant {tenant_id}")


async def _user(repo: SqlAuthRepo, email: str, tenant_id: str = TENANT) -> UserAccount:
    return await repo.create_user(
        tenant_id=tenant_id,
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password("pw"),
        role="member",
    )


@pytest.mark.db
async def test_tenant_roundtrip(container):
    await _make_tenant(container.auth, OTHER_TENANT)
    row = await container.auth.get_tenant(OTHER_TENANT)
    assert row is not None and row.name == f"Tenant {OTHER_TENANT}"
    assert await container.auth.get_tenant("nope") is None


@pytest.mark.db
async def test_users_crud_and_email_uniqueness(container):
    repo = container.auth
    user = await _user(repo, "alice@example.com")
    assert user.role == "member" and user.password_hash is not None

    assert await repo.get_user(user.id) == user
    assert await repo.get_user_by_email("alice@example.com") == user
    assert await repo.get_user_by_email("nobody@example.com") is None

    # email is globally unique (even across tenants)
    await _make_tenant(repo, OTHER_TENANT)
    with pytest.raises(IntegrityError):
        await repo.create_user(tenant_id=OTHER_TENANT, email="alice@example.com")

    patched = await repo.update_user(user.id, display_name="Alice", role="admin")
    assert patched is not None
    assert patched.display_name == "Alice" and patched.role == "admin"

    members = await repo.list_users(TENANT)
    assert [u.id for u in members] == [user.id]
    assert await repo.list_users(OTHER_TENANT) == []

    assert await repo.delete_user(user.id) is True
    assert await repo.get_user(user.id) is None


@pytest.mark.db
async def test_delete_user_refused_when_they_own_keys_or_credentials(container):
    repo = container.auth
    user = await _user(repo, "owner2@example.com")
    await repo.create_api_key(
        tenant_id=TENANT,
        user_id=user.id,
        name="k",
        key_hash=hash_api_key(generate_api_key()),
        key_prefix=key_prefix("jarvis_sk_x"),
    )
    # keys block deletion …
    assert await repo.delete_user(user.id) is False
    assert await repo.get_user(user.id) is not None

    creds_user = await _user(repo, "cred@example.com")
    await repo.create_credential(
        tenant_id=TENANT,
        name="c",
        provider="openai_compatible",
        ciphertext={"v": "1", "key_id": "x", "nonce": "n", "ct": "c"},
        created_by=creds_user.id,
    )
    assert await repo.delete_user(creds_user.id) is False

    # … but a clean user goes through
    clean = await _user(repo, "clean@example.com")
    assert await repo.delete_user(clean.id) is True


@pytest.mark.db
async def test_session_lookup_is_hash_and_expiry_gated(container):
    repo = container.auth
    user = await _user(repo, "sess@example.com")
    token = "0" * 64  # caller hashes the presented cookie; repo stores only the hash
    await repo.create_session(
        user_id=user.id, token_hash=token, expires_at=datetime.now(UTC) + timedelta(days=30)
    )
    hit = await repo.get_session_by_token_hash(token)
    assert hit is not None
    record, loaded_user = hit
    assert record.user_id == user.id and loaded_user.email == "sess@example.com"

    await repo.delete_session(record.id)
    assert await repo.get_session_by_token_hash(token) is None

    # an expired session does not authenticate
    await repo.create_session(
        user_id=user.id, token_hash="e" * 64, expires_at=datetime.now(UTC) - timedelta(days=1)
    )
    assert await repo.get_session_by_token_hash("e" * 64) is None


@pytest.mark.db
async def test_api_keys_hash_lookup_revocation_and_tenant_scope(container):
    repo = container.auth
    user = await _user(repo, "keyer@example.com")
    plaintext = generate_api_key()
    record = await repo.create_api_key(
        tenant_id=TENANT,
        user_id=user.id,
        name="cli",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    assert isinstance(record, ApiKeyRecord)
    assert record.key_prefix == plaintext[:12] and record.key_prefix != plaintext

    hit = await repo.get_api_key_by_hash(hash_api_key(plaintext))
    assert hit is not None
    key, loaded_user = hit
    assert key.id == record.id and loaded_user.id == user.id
    # a lookup counts as a use — last_used_at is stamped
    again = await repo.get_api_key_by_hash(hash_api_key(plaintext))
    assert again is not None and again[0].last_used_at is not None

    # revocation is tenant-scoped: the wrong tenant sees nothing (404 path)
    assert await repo.revoke_api_key(record.id, OTHER_TENANT) is False
    assert await repo.revoke_api_key(record.id, TENANT) is True
    assert await repo.get_api_key_by_hash(hash_api_key(plaintext)) is None  # revoked

    keys = await repo.list_api_keys(TENANT)
    assert [k.id for k in keys] == [record.id] and keys[0].revoked_at is not None


@pytest.mark.db
async def test_credentials_tenant_scoped_and_write_only(container):
    repo = container.auth
    user = await _user(repo, "byok@example.com")
    await _make_tenant(repo, OTHER_TENANT)

    envelope = {"v": "1", "key_id": "abc", "nonce": "n", "ct": "c"}
    record = await repo.create_credential(
        tenant_id=TENANT,
        name="prod-key",
        provider="openai_compatible",
        ciphertext=envelope,
        created_by=user.id,
    )
    assert isinstance(record, StoredCredentialRecord)
    assert record.ciphertext == envelope  # envelope passthrough, no plaintext field

    # tenant-scoped read: same tenant sees it, the other tenant gets None
    assert await repo.get_credential(record.id, TENANT) == record
    assert await repo.get_credential(record.id, OTHER_TENANT) is None
    assert await repo.get_credential("missing", TENANT) is None

    new_envelope = {"v": "1", "key_id": "def", "nonce": "n2", "ct": "c2"}
    patched = await repo.update_credential(
        record.id, TENANT, name="renamed", ciphertext=new_envelope
    )
    assert patched is not None
    assert patched.name == "renamed" and patched.ciphertext == new_envelope
    # the other tenant cannot patch either
    assert await repo.update_credential(record.id, OTHER_TENANT, name="steal") is None

    assert await repo.revoke_credential(record.id, OTHER_TENANT) is False
    assert await repo.revoke_credential(record.id, TENANT) is True
    revoked = await repo.get_credential(record.id, TENANT)
    assert revoked is not None and revoked.revoked_at is not None

    creds = await repo.list_credentials(TENANT)
    assert [c.id for c in creds] == [record.id]

    # deleting the creator is refused while the credential exists
    assert await repo.delete_user(user.id) is False
