"""Tool domain types — pure Pydantic (+ stdlib CancellationToken), no IO."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.execution import CancellationToken


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolDescriptor(_Model):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON Schema
    annotations: dict[str, Any] = Field(default_factory=dict)  # anticipates authorization flags


class ToolResult(_Model):
    tool_call_id: str
    tool_name: str
    output: str
    is_error: bool = False
    latency_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolError(_Model):
    kind: Literal["validation", "timeout", "internal"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolContext:
    """Mutable per-run context handed to tool executions. Anticipates
    principal/secrets without carrying them yet."""

    def __init__(
        self,
        run_id: str,
        agent_id: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        variables: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.user_id = user_id
        self.variables = variables or {}
        self.config = config or {}
        self.cancel = cancel or CancellationToken()

    def raise_if_cancelled(self) -> None:
        self.cancel.raise_if_triggered()


__all__ = ["CancellationToken", "ToolContext", "ToolDescriptor", "ToolError", "ToolResult"]