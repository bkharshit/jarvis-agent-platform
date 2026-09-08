"""McpToolProvider — eager per-segment MCP toolset resolution (S4, D38).

Groups the agent's enabled `mcp__*` bindings by server, connects each
referenced server (tenant-scoped rows, owned shadowing shared), and builds
a per-run registry view: builtins + one McpTool per BOUND discovered tool.
Discovery is not exposure — a server's unbound tools are never registered,
so a model cannot reach them even by hallucinated name. Connections close
with the segment via `McpTooling.aclose` (finally: complete/fail/cancel/
pause); resume segments re-resolve.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jarvis.config import Settings
from jarvis.domain.agent import ToolBinding
from jarvis.domain.mcp import McpServer
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.ports.repository import McpServerRepo
from jarvis.ports.tools import ToolRegistry
from jarvis.tools.base import BaseTool
from jarvis.tools.mcp.connection import McpServerConnection
from jarvis.tools.mcp.errors import McpResolutionError
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime

MCP_PREFIX = "mcp__"


def _split_binding_name(name: str) -> tuple[str, str] | None:
    """`mcp__<server>__<tool>` → (server, raw tool name). Server names are
    slugs (no underscores), so the split after the prefix is unambiguous;
    non-mcp__ names return None."""
    if not name.startswith(MCP_PREFIX):
        return None
    rest = name[len(MCP_PREFIX) :]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


@dataclass
class McpTooling:
    """The per-segment toolset the runtime threads through execute/loop."""

    registry: ToolRegistry
    tool_runtime: ToolRuntime
    aclose: Callable[[], Awaitable[None]]


class McpTool(BaseTool):
    """One discovered MCP tool as an ordinary BaseTool — the ToolRuntime
    envelope (validation/timeout/cancellation/truncation) applies for free."""

    def __init__(
        self, descriptor: ToolDescriptor, connection: McpServerConnection, raw_name: str
    ) -> None:
        super().__init__(descriptor)
        self._connection = connection
        self._raw_name = raw_name

    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return await self._connection.call(self._raw_name, arguments)


ConnectionFactory = Callable[[McpServer], McpServerConnection]


class McpToolProvider:
    """The runtime's MCP seam. `connection_factory` is the unit-test seam —
    the default builds real SDK connections."""

    def __init__(
        self,
        repo: McpServerRepo,
        base_registry: ToolRegistry,
        settings: Settings,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self._repo = repo
        self._base_registry = base_registry
        self._settings = settings
        self._connection_factory: ConnectionFactory = connection_factory or (
            lambda server: McpServerConnection(server, settings.mcp_connect_timeout)
        )

    async def resolve(self, bindings: list[ToolBinding], *, tenant_id: str | None) -> McpTooling:
        """Build the segment's registry view. No `mcp__*` bindings → the
        builtin registry with nothing open (byte-identical behavior to
        today). Any resolution failure raises McpResolutionError naming the
        server — the runtime converts it into the persisted terminal."""
        requested: dict[str, set[str]] = {}
        for binding in bindings:
            if not binding.enabled:
                continue
            split = _split_binding_name(binding.name)
            if split is None:
                continue
            requested.setdefault(split[0], set()).add(split[1])
        if not requested:
            return self._default_tooling()

        connections: list[McpServerConnection] = []
        try:
            view = self._registry_view()
            for server_name, bound_tools in requested.items():
                server = await self._repo.get_by_name(server_name, tenant_id=tenant_id)
                if server is None:
                    raise McpResolutionError(
                        server_name,
                        "no configured server with this name (bound tool would never resolve)",
                    )
                if not server.enabled:
                    raise McpResolutionError(server_name, "the server is disabled")
                connection = await self._connect(server)
                connections.append(connection)
                server_prefix = f"mcp__{server_name}__"
                for descriptor in await connection.descriptors():
                    tool_part = descriptor.name[len(server_prefix) :]
                    if tool_part not in bound_tools:
                        continue  # discovery is not exposure: only BOUND tools register
                    view.register(
                        McpTool(descriptor, connection, connection.raw_names[descriptor.name])
                    )
                # A bound tool missing from discovery is drift — it degrades
                # like an unknown builtin (D37 §2): skipped at prompt build;
                # a call returns the ToolRuntime's "unknown tool" error.
            return McpTooling(
                registry=view,
                tool_runtime=ToolRuntime(view),
                aclose=_Close(connections),
            )
        except Exception:
            for connection in connections:
                await connection.close()
            raise

    async def probe(self, server: McpServer) -> list[ToolDescriptor]:
        """Connect fresh and list tools, persisting nothing (the API probe
        route). Resolution failures propagate — the route maps them to
        502 mcp_unreachable."""
        connection = self._connection_factory(server)
        try:
            await connection.connect()
            return await connection.descriptors()
        finally:
            await connection.close()

    def _registry_view(self) -> InMemoryToolRegistry:
        view = InMemoryToolRegistry()
        for tool in self._base_registry.descriptors():
            view.register(self._base_registry.get(tool.name))
        return view

    def _default_tooling(self) -> McpTooling:
        return McpTooling(
            registry=self._base_registry,
            tool_runtime=ToolRuntime(self._base_registry),
            aclose=_no_op_close,
        )

    async def _connect(self, server: McpServer) -> McpServerConnection:
        connection = self._connection_factory(server)
        await connection.connect()
        return connection


async def _no_op_close() -> None: ...


class _Close:
    """Close every segment connection exactly once, first-error tolerant."""

    def __init__(self, connections: list[McpServerConnection]) -> None:
        self._connections = connections

    async def __call__(self) -> None:
        errors: list[Exception] = []
        for connection in self._connections:
            try:
                await connection.close()
            except Exception as exc:  # noqa: BLE001 — never block the other closes
                errors.append(exc)
        if errors:
            raise errors[0]


__all__ = ["MCP_PREFIX", "McpTool", "McpToolProvider", "McpTooling", "_split_binding_name"]
