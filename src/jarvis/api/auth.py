"""Request authentication (S2, ADR 0009 §4): credentials → Principal →
scoped repo views.

Resolution order:
1. `Authorization: Bearer jarvis_sk_…` — hashed, looked up in api_keys
   (unrevoked), stamped last_used_at → Principal(mode="api_key").
2. `jarvis_session` cookie — hashed, looked up in sessions (unexpired) →
   Principal(mode="session").
3. No credentials + auth_mode=anonymous → Principal on the default tenant
   (the pre-S2 behavior; local dev and the CLI stay friction-free).
4. auth_mode=required with no credentials → 401 envelope.

A *presented but invalid* credential is always a 401 — falling back to
anonymous would silently mask revocation. The dependency hands routes an
`AuthContext`: the Principal plus tenant-scoped repo views, so controllers
never touch raw (unscoped) repos for tenant-owned data.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.domain.auth import Principal, UserAccount
from jarvis.persistence.models import DEFAULT_TENANT
from jarvis.persistence.repositories import SqlAuthRepo
from jarvis.persistence.scoped import (
    TenantScopedAgents,
    TenantScopedConversations,
    TenantScopedExecutions,
)
from jarvis.security import hash_api_key, is_api_key

SESSION_COOKIE = "jarvis_session"
SESSION_TTL_DAYS = 30

# Module-level Depends singleton (ruff B008), matching the ContainerDep
# pattern in the route modules.
ContainerDep = Depends(get_container)


@dataclass
class AuthContext:
    """The per-request identity bundle routes consume instead of
    `container.*` for tenant-owned data. `repo` is the raw auth repo — every
    user/api-key/credential call on it takes the principal's tenant_id
    explicitly, so scoping stays visible at the call site."""

    principal: Principal
    user: UserAccount | None  # None for the anonymous principal
    agents: TenantScopedAgents
    executions: TenantScopedExecutions
    conversations: TenantScopedConversations
    repo: SqlAuthRepo

    @classmethod
    def build(
        cls,
        container: AppContainer,
        principal: Principal,
        user: UserAccount | None = None,
    ) -> AuthContext:
        return cls(
            principal=principal,
            user=user,
            agents=TenantScopedAgents(container.agents, principal.tenant_id),
            executions=TenantScopedExecutions(container.executions, principal.tenant_id),
            conversations=TenantScopedConversations(container.conversations, principal.tenant_id),
            repo=container.auth,
        )


async def _resolve(
    request: Request, container: AppContainer
) -> tuple[Principal, UserAccount | None]:
    """Credential material → (Principal, user). 401s on presented-but-invalid
    credentials; anonymous fallback only when nothing was presented."""
    header = request.headers.get("authorization", "")
    if header:
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not is_api_key(token):
            raise ApiError(401, "unauthenticated", "unsupported authorization scheme")
        key_hit = await container.auth.get_api_key_by_hash(hash_api_key(token))
        if key_hit is None:
            raise ApiError(401, "unauthenticated", "invalid or revoked API key")
        key, user = key_hit
        return (
            Principal(
                tenant_id=key.tenant_id,
                user_id=user.id,
                api_key_id=key.id,
                role=user.role,
                mode="api_key",
            ),
            user,
        )

    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session_hit = await container.auth.get_session_by_token_hash(hash_api_key(cookie))
        if session_hit is None:
            raise ApiError(401, "unauthenticated", "invalid or expired session")
        record, user = session_hit
        return (
            Principal(
                tenant_id=user.tenant_id,
                user_id=user.id,
                role=user.role,
                mode="session",
            ),
            user,
        )

    if container.settings.auth_mode == "anonymous":
        return Principal(tenant_id=DEFAULT_TENANT, mode="anonymous"), None
    raise ApiError(401, "unauthenticated", "authentication required — log in or present an API key")


async def resolve_principal(request: Request, container: AppContainer) -> Principal:
    return (await _resolve(request, container))[0]


async def get_auth_context(request: Request, container: AppContainer = ContainerDep) -> AuthContext:
    principal, user = await _resolve(request, container)
    return AuthContext.build(container, principal, user)


# Defined after get_auth_context — Depends needs the resolved function.
AuthDep = Depends(get_auth_context)


__all__ = [
    "AuthContext",
    "AuthDep",
    "SESSION_COOKIE",
    "SESSION_TTL_DAYS",
    "get_auth_context",
    "resolve_principal",
]
