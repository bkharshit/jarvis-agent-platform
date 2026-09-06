"""Credential resolution — Protocols ONLY (pydantic/stdlib imports only).

ADR 0006: agents hold credential *references*, never material; a
`CredentialResolver` owns materialization behind `ModelProviderFactory`
(the single seam between "what an agent references" and "how the key
materializes"). Implementations:

- env resolver (self-hosted default) — preserves D18's lazy env-var
  indirection;
- stored/encrypted resolver (hosted BYOK) — lands with the credentials
  table (S2).

Tenant-scoping invariant (ADR 0006 §10): **a credential id alone is never
sufficient authorization.** Stored resolution takes a `Principal` and
verifies tenant ownership; without a principal, stored references do not
resolve.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.agent import CredentialRef
from jarvis.domain.auth import Principal


class _Resolved(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolvedEnv(_Resolved):
    """D18 preserved: an env-var *name* — the key is read from the process
    environment lazily at request time, never materialized early."""

    type: Literal["env"]
    env_var: str


class ResolvedMaterial(_Resolved):
    """Short-lived key material (hosted BYOK). Lives only inside the
    per-resolve provider instance — never persisted, logged, or returned."""

    type: Literal["material"]
    value: str


ResolvedCredential = Annotated[ResolvedEnv | ResolvedMaterial, Field(discriminator="type")]


class CredentialError(Exception):
    """Resolution failed: reference unknown to this principal's tenant,
    revoked, or the deployment has no resolver for this kind."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@runtime_checkable
class CredentialResolver(Protocol):
    """Materializes a credential reference for a principal.

    `principal=None` is valid only for env references (which carry no
    ownership) — stored references require a principal and resolve
    tenant-scoped, never across tenants. Resolution is async: the stored
    backend (S2) reads the tenant-scoped credential row from the DB
    (ADR 0009 §7)."""

    async def resolve(
        self, principal: Principal | None, ref: CredentialRef
    ) -> ResolvedCredential: ...


__all__ = [
    "CredentialError",
    "CredentialResolver",
    "ResolvedCredential",
    "ResolvedEnv",
    "ResolvedMaterial",
]
