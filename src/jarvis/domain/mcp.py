"""MCP server definitions (S4, ADR 0012) — pure Pydantic, no IO.

Servers are tenant-scoped platform rows (D37); agents bind discovered tools
by name (`mcp__<server>__<tool>`), so this module carries only the server's
own identity + connectivity config. Config validation at the domain edge
gives both the API its 422 and the repo a typed boundary (ADR 0002 pattern).

Secrets stay references (ADR 0005): the config union carries env-var *names*
(`EnvCredentialRef`), never values.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.agent import EnvCredentialRef


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpStdioConfig(_Model):
    """A server launched as a local subprocess (the fixture server's shape)."""

    type: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, EnvCredentialRef] = Field(default_factory=dict)


class McpHttpConfig(_Model):
    """A streamable-HTTP server. Headers carry env-var references — the
    resolved values are used at connect time and never stored."""

    type: Literal["http"]
    url: str
    headers: dict[str, EnvCredentialRef] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"url must be an http(s) URL, got {value!r}")
        return value


McpServerConfig = Annotated[McpStdioConfig | McpHttpConfig, Field(discriminator="type")]

# ADR 0012 §1: the name joins thousands of version snapshots into
# `mcp__<name>__<tool>`, so it is a strict slug.
MCP_NAME_SLUG = r"^[a-z0-9][a-z0-9-]*$"


class McpServer(_Model):
    """One configured MCP server. `tenant_id=None` = platform-shared
    (visible to every tenant, the agents-repo convention); name is immutable
    once created — it is the join key from agent version snapshots."""

    id: str
    name: str
    config: McpServerConfig
    enabled: bool = True
    tenant_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        if not re.fullmatch(MCP_NAME_SLUG, value):
            raise ValueError(
                f"server name must be a slug matching {MCP_NAME_SLUG} (got {value!r})"
                " — it becomes part of every tool name mcp__<name>__<tool>"
            )
        return value


__all__ = [
    "MCP_NAME_SLUG",
    "McpHttpConfig",
    "McpServer",
    "McpServerConfig",
    "McpStdioConfig",
]
