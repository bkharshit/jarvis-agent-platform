"""Explicit ToolRegistry — no import side effects; duplicates raise."""

from __future__ import annotations

from jarvis.domain.tools import ToolDescriptor
from jarvis.ports.tools import Tool


class ToolNotFoundError(KeyError):
    pass


class DuplicateToolError(ValueError):
    pass


class InMemoryToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.descriptor.name
        if name in self._tools:
            raise DuplicateToolError(f"tool already registered: {name!r}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"tool not registered: {name!r}") from None

    def descriptors(self) -> list[ToolDescriptor]:
        return [tool.descriptor for tool in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


__all__ = ["DuplicateToolError", "InMemoryToolRegistry", "ToolNotFoundError"]
