"""BYOK credential management (S2, ADR 0006 §7): create/patch/revoke stored
credentials. The secret enters in plaintext ONCE, is AES-GCM encrypted
with the master key, and never leaves the persistence layer again — GET
schemas carry no ciphertext and no secret field. A foreign tenant's
credential id is 404 (no existence leak)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import CredentialCreate, CredentialList, CredentialOut, CredentialPatch
from jarvis.domain.auth import StoredCredentialRecord
from jarvis.security import CryptoError, encrypt, load_master_key

router = APIRouter(prefix="/credentials", tags=["credentials"])

# Module-level Depends singleton (ruff B008).
ContainerDep = Depends(get_container)


def _out(record: StoredCredentialRecord) -> CredentialOut:
    # built field-by-field — ciphertext and secret can never ride along
    return CredentialOut(
        id=record.id,
        tenant_id=record.tenant_id,
        name=record.name,
        provider=record.provider,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
        revoked_at=record.revoked_at,
    )


def _master_key(container: AppContainer) -> bytes:
    """The master key is referenced by env-var name (D18); a missing key is
    a deployment problem, not a 500."""
    try:
        return load_master_key(container.settings)
    except CryptoError as exc:
        raise ApiError(
            503,
            "credentials_unavailable",
            f"stored credentials are not configured: {exc.message}",
        ) from None


@router.get("")
async def list_credentials(auth: AuthContext = AuthDep) -> CredentialList:
    records = await auth.repo.list_credentials(auth.principal.tenant_id)
    return CredentialList(items=[_out(r) for r in records])


@router.post("", status_code=201)
async def create_credential(
    req: CredentialCreate,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> CredentialOut:
    if auth.user is None:
        raise ApiError(403, "forbidden", "credentials require an authenticated principal")
    envelope = encrypt(req.secret, master_key=_master_key(container))
    record = await auth.repo.create_credential(
        tenant_id=auth.principal.tenant_id,
        name=req.name,
        provider=req.provider,
        ciphertext=envelope,
        created_by=auth.user.id,
    )
    return _out(record)


@router.patch("/{credential_id}")
async def update_credential(
    credential_id: str,
    req: CredentialPatch,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> CredentialOut:
    envelope = encrypt(req.secret, master_key=_master_key(container)) if req.secret else None
    updated = await auth.repo.update_credential(
        credential_id,
        auth.principal.tenant_id,
        name=req.name,
        ciphertext=envelope,
    )
    if updated is None:
        raise ApiError(404, "not_found", f"credential {credential_id!r} not found")
    return _out(updated)


@router.delete("/{credential_id}", status_code=204)
async def revoke_credential(credential_id: str, auth: AuthContext = AuthDep) -> None:
    revoked = await auth.repo.revoke_credential(credential_id, auth.principal.tenant_id)
    if not revoked:
        raise ApiError(404, "not_found", f"credential {credential_id!r} not found")


__all__ = ["router"]
