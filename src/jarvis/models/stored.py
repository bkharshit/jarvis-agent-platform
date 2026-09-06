"""DB-backed stored-credential resolver — the S2 half of ADR 0006 §4.

Implements `StoredResolver`: resolves a `StoredCredentialRef` for a
Principal, tenant-scoped (a credential id alone is never sufficient
authorization). The AES-GCM envelope is decrypted only at resolve time and
materializes as a short-lived `ResolvedMaterial` that never persists,
logs, or crosses the API. Missing/revoked/foreign → `CredentialError`
(the route layer's 404-equivalent — no existence leak); tampered
ciphertext → `CredentialError` too, so a corrupted envelope becomes a run
failure (D5), never an exception past the runtime.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config import Settings
from jarvis.domain.agent import StoredCredentialRef
from jarvis.domain.auth import Principal, StoredCredentialRecord
from jarvis.persistence.repositories import SqlAuthRepo
from jarvis.ports.credential import CredentialError, ResolvedMaterial
from jarvis.security import CryptoError, decrypt, load_master_key


class DatabaseStoredResolver:
    """Reads the tenant-scoped credential row, decrypts the envelope with
    the master key (referenced by env-var name — D18), and hands back
    short-lived material. The master key is loaded lazily and cached: the
    env var does not change under a running process."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._settings = settings
        self._master_key: bytes | None = None
        self._auth = SqlAuthRepo(sessionmaker)

    def _key(self) -> bytes:
        if self._master_key is None:
            self._master_key = load_master_key(self._settings)
        return self._master_key

    async def resolve(self, principal: Principal, ref: StoredCredentialRef) -> ResolvedMaterial:
        record: StoredCredentialRecord | None = await self._auth.get_credential(
            ref.credential_id, principal.tenant_id
        )
        # Foreign tenant, revoked, or absent — one indistinguishable failure
        # (ADR 0006 §10: no existence leak). The message names the id the
        # caller supplied, nothing else.
        if record is None or record.revoked_at is not None:
            raise CredentialError(f"credential {ref.credential_id!r} not found")
        try:
            secret = decrypt(record.ciphertext, master_key=self._key())
        except CryptoError as exc:
            # Tampered or wrong-key ciphertext fails loudly (ADR 0006) — as a
            # CredentialError, so the run ends in a persisted model failure
            # (D5) instead of an exception past the runtime.
            raise CredentialError(
                f"credential {ref.credential_id!r} failed to decrypt: {exc.message}"
            ) from None
        return ResolvedMaterial(type="material", value=secret)


__all__ = ["DatabaseStoredResolver"]
