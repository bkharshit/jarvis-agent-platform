"""MCP adapter internals (S4, ADR 0012 §6) — the ONLY module that touches
the mcp SDK. The runtime, API, and tests never import it directly; unit
tests fake the connection seam, so the SDK can be swapped without touching
the runtime (adapters swappable by construction)."""

from __future__ import annotations


class McpResolutionError(Exception):
    """The one internal exception the runtime catches before its blanket
    handler (D38): a bound MCP server is missing, disabled, unreachable, or
    its auth env var is absent. Becomes exactly one persisted terminal
    `run.failed` with error_kind="tool" naming the server."""

    def __init__(self, server_name: str, detail: str) -> None:
        super().__init__(f"MCP server {server_name!r}: {detail}")
        self.server_name = server_name


__all__ = ["McpResolutionError"]
