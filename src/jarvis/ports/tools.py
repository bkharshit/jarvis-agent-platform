"""Tool system protocols — Protocols ONLY."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext, ToolDescriptor, ToolResult


@runtime_checkable
class Tool(Protocol):
    """A tool implementation. Follows the template method: validation and
    normalization are handled by the ToolRuntime around `execute`."""

    @property
    def descriptor(self) -> ToolDescriptor: ...

    async def execute(self, arguments: dict[str, object], context: ToolContext) -> str:
        """Run the tool. Returns plain-text output. Raises on failure —
        the runtime converts that to an error ToolResult, never a crash."""
        ...


class ToolRegistry(Protocol):
    """Explicit registry — no import side effects, duplicates raise."""

    def register(self, tool: Tool) -> None: ...

    def get(self, name: str) -> Tool: ...

    def descriptors(self) -> list[ToolDescriptor]: ...


class ToolRuntime(Protocol):
    """Safe execution envelope: validate arguments against the descriptor's
    JSON Schema, enforce timeout/cancellation, never crash the run,
    emit tool.* events."""

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult: ...


__all__ = ["Tool", "ToolRegistry", "ToolRuntime"]