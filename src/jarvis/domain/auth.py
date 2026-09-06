"""Authenticated identity — pure Pydantic, no IO (S2, ADR 0009 §1).

Auth is a transport concern: the API resolves a request's credentials
(session cookie or API key) into a `Principal`, and everything inward
(repos, queue message, `ExecutionContext`, credential resolution) speaks
only in Principals. Nothing below the API layer knows about HTTP.
"""

from __future__ import annotations

from typing import Literal

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


__all__ = ["AuthMode", "Principal", "TenantRole"]
