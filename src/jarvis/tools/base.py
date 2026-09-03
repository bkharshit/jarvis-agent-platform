"""Tool base class — template method (Dify's `Tool.invoke()` pattern, minus
the god-class facade).

Subclasses implement `_execute` only; validation, timeout, cancellation and
error conversion live in the ToolRuntime envelope (`tools/runtime.py`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from jarvis.domain.tools import ToolContext, ToolDescriptor


class BaseTool(ABC):
    """Concrete tools subclass this and implement `_execute`."""

    def __init__(self, descriptor: ToolDescriptor, config: dict[str, Any] | None = None) -> None:
        self._descriptor = descriptor
        self.config: dict[str, Any] = dict(config or {})

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    @abstractmethod
    async def _execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Do the actual work. Return plain-text output."""

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Public entry — subclasses rarely override; the runtime wraps this."""
        return await self._execute(arguments, context)


__all__ = ["BaseTool"]
