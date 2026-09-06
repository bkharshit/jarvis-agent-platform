"""Credential resolvers — the factory-facing implementations of the port.

ADR 0006 §4: multiple resolvers behind one Protocol; provider and runtime
code do not fork. `EnvCredentialResolver` is the self-hosted default (env
refs only); `DefaultCredentialResolver` composes it with an optional
stored-credential backend (wired in S2 when the credentials table lands).
"""

from __future__ import annotations

from typing import Protocol

from jarvis.domain.agent import EnvCredentialRef, StoredCredentialRef
from jarvis.domain.auth import Principal
from jarvis.ports.credential import (
    CredentialError,
    ResolvedCredential,
    ResolvedEnv,
    ResolvedMaterial,
)


class StoredResolver(Protocol):
    """The stored-credential backend (the credentials table, S2):
    tenant-scoped by contract — it resolves only credentials owned by
    `principal.tenant_id`. Async: resolution reads the credential row."""

    async def resolve(self, principal: Principal, ref: StoredCredentialRef) -> ResolvedMaterial: ...


class EnvCredentialResolver:
    """Self-hosted default: env references only, no storage anywhere."""

    async def resolve(
        self, principal: Principal | None, ref: EnvCredentialRef | StoredCredentialRef
    ) -> ResolvedCredential:
        if isinstance(ref, EnvCredentialRef):
            return ResolvedEnv(type="env", env_var=ref.env_var)
        raise CredentialError("stored credentials are not available in this deployment")


class DefaultCredentialResolver:
    """Dispatches by reference kind: env refs resolve from the environment;
    stored refs delegate to the (optional) stored backend — tenant-scoped
    and principal-required (ADR 0006 §10). A stored ref with no stored
    backend, or without a principal, is a CredentialError, never a fallback."""

    def __init__(self, stored_resolver: StoredResolver | None = None) -> None:
        # The stored backend composes in with the credentials table (S2).
        self._stored = stored_resolver

    async def resolve(
        self, principal: Principal | None, ref: EnvCredentialRef | StoredCredentialRef
    ) -> ResolvedCredential:
        if isinstance(ref, EnvCredentialRef):
            return ResolvedEnv(type="env", env_var=ref.env_var)
        if self._stored is None:
            raise CredentialError("stored credentials are not available in this deployment")
        if principal is None:
            raise CredentialError(
                "a principal is required to resolve a stored credential — "
                "a credential id alone is never sufficient authorization"
            )
        return await self._stored.resolve(principal, ref)


__all__ = ["DefaultCredentialResolver", "EnvCredentialResolver", "StoredResolver"]
