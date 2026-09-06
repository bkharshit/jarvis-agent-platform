"""Auth surface (S2, ADR 0009 §4): login (session cookie), logout, whoami.

Sessions are server-side rows; the cookie carries only an opaque random
token (hashed at rest — a stolen sessions table yields no tokens). The
cookie is httpOnly + SameSite=Lax so the browser never exposes it to JS
and cross-site POSTs can't ride it.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response

from jarvis.api.auth import SESSION_COOKIE, SESSION_TTL_DAYS, AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import LoginRequest, WhoamiResponse
from jarvis.domain.auth import UserAccount
from jarvis.security import hash_api_key, hash_password, verify_password

router = APIRouter(tags=["auth"])

# Module-level Depends singleton (ruff B008).
ContainerDep = Depends(get_container)

# Burned on unknown-email logins so response timing can't enumerate users.
_DUMMY_HASH = hash_password("jarvis-dummy-password-for-timing-parity")


def _whoami_response(
    user: UserAccount | None, principal_mode: str, tenant_id: str, role: str | None
) -> WhoamiResponse:
    return WhoamiResponse(
        tenant_id=tenant_id,
        user_id=user.id if user else None,
        email=user.email if user else None,
        display_name=user.display_name if user else "",
        role=role,
        mode=principal_mode,
    )


@router.post("/auth/login")
async def login(
    req: LoginRequest, response: Response, container: AppContainer = ContainerDep
) -> WhoamiResponse:
    user = await container.auth.get_user_by_email(req.email)
    if user is None or user.password_hash is None:
        verify_password(req.password, _DUMMY_HASH)  # same cost as the miss path
        raise ApiError(401, "unauthenticated", "invalid email or password")
    if not verify_password(req.password, user.password_hash):
        raise ApiError(401, "unauthenticated", "invalid email or password")

    token = secrets.token_hex(32)  # opaque; only its hash is stored
    expires = datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS)
    await container.auth.create_session(
        user_id=user.id, token_hash=hash_api_key(token), expires_at=expires
    )
    # `secure` is off so plain-http local dev works; TLS-terminating
    # deployments should front it with HSTS (S2 scope note).
    response.set_cookie(
        SESSION_COOKIE, token, expires=expires, httponly=True, samesite="lax", path="/"
    )
    return _whoami_response(user, "session", user.tenant_id, user.role)


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request, response: Response, container: AppContainer = ContainerDep
) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        hit = await container.auth.get_session_by_token_hash(hash_api_key(token))
        if hit is not None:
            await container.auth.delete_session(hit[0].id)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/whoami")
async def whoami(auth: AuthContext = AuthDep) -> WhoamiResponse:
    principal = auth.principal
    user = auth.user
    return _whoami_response(user, principal.mode, principal.tenant_id, principal.role)


__all__ = ["router"]
