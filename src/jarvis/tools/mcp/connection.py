"""McpServerConnection — one MCP server connection (S4, ADR 0012 §3).

An async context manager over the SDK v2 unified `Client`. Credential refs
resolve at connect time (ADR 0013): env refs from the process env, stored
refs through the injected `CredentialResolver` (AES-GCM decrypt, tenant-
scoped); values are used, never stored. Stdio subprocesses get the SDK's
minimal default environment (PATH/HOME/…) plus the referenced variables —
never the parent's wholesale environment.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ListToolsResult,
    ResourceLink,
    TextContent,
)

from jarvis.domain.agent import CredentialRef, EnvCredentialRef, StoredCredentialRef
from jarvis.domain.auth import Principal
from jarvis.domain.mcp import McpHttpConfig, McpServer, McpStdioConfig
from jarvis.domain.tools import ToolDescriptor
from jarvis.ports.credential import (
    CredentialError,
    CredentialResolver,
    ResolvedEnv,
)
from jarvis.tools.mcp.errors import McpResolutionError

# Tool names are sanitized to [A-Za-z0-9_-] (ADR 0012 §2): other characters
# collapse to `_`, first-wins on collisions.
_SANITIZE = re.compile(r"[^A-Za-z0-9_-]")

_NON_TEXT_SUMMARY: dict[type[Any], str] = {
    ImageContent: "[image content omitted]",
    AudioContent: "[audio content omitted]",
    ResourceLink: "[resource link omitted]",
    EmbeddedResource: "[embedded resource omitted]",
}


def _sanitize(raw: str, seen: set[str]) -> str:
    name = _SANITIZE.sub("_", raw) or "_"
    candidate, suffix = name, 1
    while candidate in seen:  # first-wins; later collisions get a suffix
        candidate = f"{name}_{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _root_cause(exc: BaseException) -> BaseException:
    """The SDK/anyio wraps handshake failures in TaskGroup ExceptionGroups —
    the useful error is the innermost one (an MCPError, an httpx status),
    not the group's sub-exception count."""
    seen = exc
    while isinstance(seen, BaseExceptionGroup):
        subs = seen.exceptions
        non_groups = [s for s in subs if not isinstance(s, BaseExceptionGroup)]
        seen = non_groups[0] if non_groups else subs[0]
    return seen


class McpServerConnection:
    """The connection seam. Unit tests substitute their own objects for
    this class; the provider only needs the async-CM + descriptors/call
    surface. This real implementation wraps the SDK's unified Client."""

    def __init__(
        self,
        server: McpServer,
        connect_timeout: float,
        *,
        credential_resolver: CredentialResolver | None = None,
        principal: Principal | None = None,
    ) -> None:
        self._server = server
        self._connect_timeout = connect_timeout
        self._credential_resolver = credential_resolver
        self._principal = principal
        self._client: Client | None = None
        self._http_client: Any | None = None  # httpx2.AsyncClient, ours to close
        self._raw_names: dict[str, str] = {}  # descriptor name -> server-side tool name

    async def __aenter__(self) -> McpServerConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            client = await self._build_client()
        except McpResolutionError:
            raise
        except Exception as exc:
            raise McpResolutionError(
                self._server.name, f"connection failed: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            # Entering the Client performs the initialize handshake — and
            # owns the stdio subprocess lifecycle for the segment.
            await asyncio.wait_for(client.__aenter__(), timeout=self._connect_timeout)
        except Exception as exc:  # noqa: BLE001 — the boundary names the failure
            await self.close()
            if isinstance(exc, McpResolutionError):
                raise
            detail = (
                f"handshake timed out after {self._connect_timeout}s"
                if isinstance(exc, TimeoutError)
                else f"handshake failed: {type(_root_cause(exc)).__name__}: {_root_cause(exc)}"
            )
            raise McpResolutionError(self._server.name, detail) from exc
        self._client = client

    async def _build_client(self) -> Client:
        config = self._server.config
        if isinstance(config, McpStdioConfig):
            # Refs resolve HERE — env vars from the process env, stored
            # credentials through the injected resolver (ADR 0013). Names
            # are what the DB stores; values never enter config, DB, logs,
            # or API.
            env = await self._resolve_env(config)
            params = StdioServerParameters(command=config.command, args=config.args, env=env)
            return Client(params, raise_exceptions=True, read_timeout_seconds=self._connect_timeout)
        headers = await self._resolve_headers(config)
        self._http_client = create_mcp_http_client(headers=headers)
        # Caller-provided httpx clients are NOT closed by the transport —
        # close() owns it.
        transport = streamable_http_client(config.url, http_client=self._http_client)
        return Client(transport, raise_exceptions=True, read_timeout_seconds=self._connect_timeout)

    async def _resolve_env(self, config: McpStdioConfig) -> dict[str, str]:
        env: dict[str, str] = {}
        for child_key, ref in config.env.items():
            env[child_key] = await self._resolve_ref(ref, f"{child_key!r}")
        return env

    async def _resolve_headers(self, config: McpHttpConfig) -> dict[str, str]:
        headers: dict[str, str] = {}
        for header, ref in config.headers.items():
            headers[header] = await self._resolve_ref(ref, f"header {header!r}")
        return headers

    async def _resolve_ref(self, ref: CredentialRef, label: str) -> str:
        """One credential ref → its live value. Env refs read the process
        env; stored refs decrypt through the resolver. Every failure is a
        McpResolutionError naming the ref — never a traceback, never
        material in a message."""
        if isinstance(ref, EnvCredentialRef):
            value = os.environ.get(ref.env_var)
            if value is None:
                raise McpResolutionError(
                    self._server.name,
                    f"environment variable {ref.env_var!r} (for {label}) is not set",
                )
            return value
        return await self._resolve_stored(ref, label)

    async def _resolve_stored(self, ref: StoredCredentialRef, label: str) -> str:
        if self._credential_resolver is None:
            raise McpResolutionError(
                self._server.name,
                f"credential {ref.credential_id!r} (for {label}) cannot be"
                " resolved — this deployment has no credential resolver",
            )
        try:
            resolved = await self._credential_resolver.resolve(self._principal, ref)
        except CredentialError as exc:
            raise McpResolutionError(
                self._server.name,
                f"credential {ref.credential_id!r} (for {label}) failed to resolve: {exc.message}",
            ) from None
        if isinstance(resolved, ResolvedEnv):
            # A resolver contract bug — never pass an env-var NAME through
            # as a header value.
            raise McpResolutionError(
                self._server.name,
                f"credential {ref.credential_id!r} (for {label}) resolved to an"
                " env reference, not a secret value",
            )
        return resolved.value

    async def descriptors(self) -> list[ToolDescriptor]:
        """tools/list mapped to JARVIS descriptors. The server name is the
        namespace; requires_approval defaults True (ADR 0012 §2 — binding
        config can ungate, S10's binding-wins rule is unchanged)."""
        assert self._client is not None
        server = self._server
        seen: set[str] = set()
        descriptors: list[ToolDescriptor] = []
        try:
            listing: ListToolsResult = await asyncio.wait_for(
                self._client.list_tools(cache_mode="bypass"), timeout=self._connect_timeout
            )
        except MCPError as exc:
            raise McpResolutionError(server.name, f"tools/list failed: {exc}") from exc
        except TimeoutError:
            raise McpResolutionError(
                server.name, f"tools/list timed out after {self._connect_timeout}s"
            ) from None
        for tool in listing.tools:
            name = f"mcp__{server.name}__{_sanitize(tool.name, seen)}"
            self._raw_names[name] = tool.name
            descriptors.append(
                ToolDescriptor(
                    name=name,
                    description=tool.description or "",
                    parameters=tool.input_schema or {"type": "object", "properties": {}},
                    annotations={"requires_approval": True, "timeout": None},
                )
            )
        return descriptors

    async def call(self, raw_tool: str, arguments: dict[str, Any]) -> str:
        """tools/call → joined text. A tool-level error result or a
        JSON-RPC MCPError both raise ValueError — the ToolRuntime envelope
        converts those into recoverable error ToolResults (D38: execution
        is not the boundary; resolution is)."""
        assert self._client is not None
        try:
            result: CallToolResult = await self._client.call_tool(raw_tool, arguments)
        except MCPError as exc:
            raise ValueError(f"MCP call failed: {exc}") from exc
        if result.is_error:
            raise ValueError(
                f"MCP tool error: {_content_text(result) or 'server reported an error'}"
            )
        return _content_text(result)

    async def close(self) -> None:
        client, self._client = self._client, None
        http_client, self._http_client = self._http_client, None
        if http_client is not None:
            await http_client.aclose()
        if client is not None:
            await client.__aexit__(None, None, None)

    @property
    def raw_names(self) -> dict[str, str]:
        return dict(self._raw_names)


def _content_text(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
            continue
        summary = _NON_TEXT_SUMMARY.get(type(block))
        parts.append(summary if summary is not None else f"[{type(block).__name__} omitted]")
    return "\n".join(parts)


__all__ = ["McpServerConnection"]
