"""API-key management (S2, ADR 0009 §2): list (metadata), create (plaintext
returned exactly once), revoke. Any member of the tenant may manage keys;
keys belong to the creating user. A foreign tenant's key id is 404."""

from __future__ import annotations

from fastapi import APIRouter

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.errors import ApiError
from jarvis.api.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyList, ApiKeyOut
from jarvis.domain.auth import ApiKeyRecord
from jarvis.security import generate_api_key, hash_api_key, key_prefix

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _out(key: ApiKeyRecord) -> ApiKeyOut:
    # built field-by-field — no plaintext, no hash can ever ride along
    return ApiKeyOut(
        id=key.id,
        tenant_id=key.tenant_id,
        user_id=key.user_id,
        name=key.name,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
    )


@router.get("")
async def list_api_keys(auth: AuthContext = AuthDep) -> ApiKeyList:
    keys = await auth.repo.list_api_keys(auth.principal.tenant_id)
    return ApiKeyList(items=[_out(k) for k in keys])


@router.post("", status_code=201)
async def create_api_key(req: ApiKeyCreate, auth: AuthContext = AuthDep) -> ApiKeyCreated:
    if auth.user is None:
        raise ApiError(
            403, "forbidden", "API keys belong to users; the anonymous principal cannot own one"
        )
    plaintext = generate_api_key()
    record = await auth.repo.create_api_key(
        tenant_id=auth.principal.tenant_id,
        user_id=auth.user.id,
        name=req.name,
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    # plaintext is returned exactly once, here, and never stored (ADR 0006 §7)
    return ApiKeyCreated(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        plaintext=plaintext,
        created_at=record.created_at,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, auth: AuthContext = AuthDep) -> None:
    """Idempotent-by-id: revoking an already-revoked key is a no-op."""
    revoked = await auth.repo.revoke_api_key(key_id, auth.principal.tenant_id)
    if not revoked:
        raise ApiError(404, "not_found", f"API key {key_id!r} not found")


__all__ = ["router"]
