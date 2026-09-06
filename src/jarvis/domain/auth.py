"""Authenticated identity — pure Pydantic, no IO (S2, ADR 0009 §1).

Auth is a transport concern: the API resolves a request's credentials
(session cookie or API key) into a `Principal`, and everything inward
(repos, queue message, `ExecutionContext`, credential resolution) speaks
only in Principals. Nothing below the API layer knows about HTTP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

AuthMode = Literal["anonymous", "session", "api_key"]
TenantRole = Literal["owner", "admin", "member"]


class Principal(BaseModel):
    """Who is acting. `tenant_id` scopes every repo view and stored
    credential resolution; `user_id` is the acting user (None only for the
    anonymous principal)."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None
    api_key_id: str | None = None
    role: TenantRole | None = None
    mode: AuthMode

    @property
    def is_anonymous(self) -> bool:
        return self.mode == "anonymous"

    @property
    def can_manage_members(self) -> bool:
        return self.role in ("owner", "admin")


class UserAccount(BaseModel):
    """A member of a tenant. `password_hash` is the stored scrypt string —
    an internal credential for login verification, never serialized in any
    API response (response schemas carry their own fields)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    email: str
    display_name: str = ""
    role: TenantRole = "member"
    created_at: datetime | None = None
    password_hash: str | None = None


class SessionRecord(BaseModel):
    """A server-side session row. Only `id`/`user_id`/`expires_at` matter
    downstream — the presented cookie token is looked up by hash and never
    re-emitted."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    expires_at: datetime
    created_at: datetime | None = None


class ApiKeyRecord(BaseModel):
    """API-key metadata — no plaintext, no hash; `key_prefix` is the
    display form (`jarvis_sk_XXXXXXXXXXXX`)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    user_id: str
    name: str
    key_prefix: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class StoredCredentialRecord(BaseModel):
    """A BYOK credential. `ciphertext` is the AES-GCM envelope
    {v, key_id, nonce, ct} — the *only* secret-bearing field, carried so
    the resolver can decrypt at use time; plaintext never exists here
    (ADR 0006 §7)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    name: str
    provider: str
    ciphertext: dict[str, Any]
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    revoked_at: datetime | None = None


__all__ = [
    "ApiKeyRecord",
    "AuthMode",
    "Principal",
    "SessionRecord",
    "StoredCredentialRecord",
    "TenantRole",
    "UserAccount",
]
