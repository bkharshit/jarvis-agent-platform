"""ToolRuntime — the safe execution envelope.

Contract: a tool can never crash the run. Validation against the descriptor's
JSON Schema, per-tool timeout, cancellation, and exception → error-result
conversion all happen here. The orchestrator emits the `tool.*` events around
this envelope (it owns the sink)."""

from __future__ import annotations

import asyncio
import time

import jsonschema

from jarvis.domain.execution import ExecutionCancelled
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext, ToolResult
from jarvis.ports.tools import Tool, ToolRegistry


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        default_timeout: float = 30.0,
        max_output_chars: int = 50_000,
    ) -> None:
        self._registry = registry
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            tool = self._registry.get(call.name)
        except KeyError as exc:
            return self._error_result(call, "internal", f"unknown tool: {exc}", started)

        kind, error = self._validate(call, tool, context)
        if error is not None:
            return self._error_result(call, kind, error, started)

        # Per-tool timeout: descriptor annotations take precedence, then the
        # run-level tool config, then the runtime default.
        tool_timeout = tool.descriptor.annotations.get("timeout")
        if tool_timeout is None:
            tool_timeout = context.config.get("timeout", self.default_timeout)
        timeout = float(tool_timeout)
        try:
            context.raise_if_cancelled()
            output = await asyncio.wait_for(
                tool.execute(dict(call.arguments), context), timeout=timeout
            )
        except TimeoutError:
            return self._error_result(call, "timeout", f"tool timed out after {timeout}s", started)
        except ExecutionCancelled:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never crash the run
            return self._error_result(call, "internal", f"{type(exc).__name__}: {exc}", started)

        if len(output) > self.max_output_chars:
            output = (
                output[: self.max_output_chars]
                + f"\n... [truncated at {self.max_output_chars} chars]"
            )
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            output=output,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _validate(self, call: ToolCall, tool: Tool, context: ToolContext) -> tuple[str, str | None]:
        schema = tool.descriptor.parameters or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            return "internal", f"tool {call.name!r} has an invalid parameter schema"
        try:
            jsonschema.validate(dict(call.arguments), schema)
        except jsonschema.ValidationError as exc:
            return "validation", f"invalid arguments for {call.name!r}: {exc.message}"
        return "validation", None

    def _error_result(self, call: ToolCall, kind: str, message: str, started: float) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            output=message,
            is_error=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            metadata={"kind": kind},
        )


__all__ = ["ToolRuntime"]
