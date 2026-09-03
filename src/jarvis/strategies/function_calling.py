"""FunctionCallingStrategy — native tool calls via the model provider.

One model invocation per `step` (ADR 0004). Streams text deltas through the
sink when the provider supports streaming, so blocking and SSE runs emit
identical event sequences. Retries rate-limit/connection errors (max 2,
exponential backoff), each attempt announced via `model.invocation.started`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jarvis.domain.events import (
    ModelInvocationCompleted,
    ModelInvocationStarted,
    TextDelta,
)
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.errors import RETRYABLE_ERRORS, ModelError
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    ToolCallDelta,
    UsageDelta,
)
from jarvis.models.types import (
    TextDelta as ModelTextDelta,
)
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient
from jarvis.ports.strategy import FinishStep, StepOutcome, ToolCallsStep

MAX_ATTEMPTS = 2  # per invocation: initial + 1 retry
BACKOFF_BASE_SECONDS = 0.5


def _new_event_kwargs(ctx: ExecutionContext) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": ctx.run_id,
        "created_at": datetime.now(UTC),
    }


class FunctionCallingStrategy:
    @property
    def name(self) -> str:
        return "function_calling"

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome:
        request = ModelRequest(
            model="",
            messages=messages,
            tools=tools or None,
            temperature=ctx.temperature,
            response_format=self._response_format(ctx),
        )
        response = await self._invoke(ctx, request, client, sink)
        assistant = response.message
        if assistant.tool_calls:
            return ToolCallsStep(assistant_message=assistant, tool_calls=assistant.tool_calls)
        return FinishStep(assistant_message=assistant, finish_reason=response.finish_reason)

    @staticmethod
    def _response_format(ctx: ExecutionContext) -> dict[str, Any] | None:
        """Native structured output for json_schema providers; json_object for
        json_mode providers (schema is in the prompt); none otherwise."""
        schema = ctx.output_schema
        if schema is None:
            return None
        if ctx.structured_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            }
        if ctx.structured_mode == "json_mode":
            return {"type": "json_object"}
        return None

    async def _invoke(
        self,
        ctx: ExecutionContext,
        request: ModelRequest,
        client: ModelClient,
        sink: EventSink,
    ) -> ModelResponse:
        last_error: ModelError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await sink.append(
                ModelInvocationStarted(**_new_event_kwargs(ctx), attempt=attempt)
            )
            try:
                if client.capabilities.streaming:
                    response = await self._invoke_streaming(ctx, request, client, sink)
                else:
                    response = await client.generate(request, cancel=ctx.cancel)
                ctx.usage = ctx.usage.plus(response.usage)
                await sink.append(
                    ModelInvocationCompleted(
                        **_new_event_kwargs(ctx),
                        usage=response.usage,
                        finish_reason=response.finish_reason,
                        model=response.model,
                    )
                )
                return response
            except RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(_backoff_delay(attempt, exc))
                    continue
                raise
        assert last_error is not None
        raise last_error

    async def _invoke_streaming(
        self,
        ctx: ExecutionContext,
        request: ModelRequest,
        client: ModelClient,
        sink: EventSink,
    ) -> ModelResponse:
        """Consume the delta stream: forward text to the sink (so blocking and
        SSE runs see the same events), accumulate tool calls and usage."""
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish_reason = "stop"
        model = request.model
        async for delta in client.stream(request, cancel=ctx.cancel):
            if isinstance(delta, ModelTextDelta):
                text_parts.append(delta.text)
                await sink.append(TextDelta(**_new_event_kwargs(ctx), text=delta.text))
            elif isinstance(delta, ToolCallDelta):
                entry = tool_calls.setdefault(delta.index, {"arguments": ""})
                if delta.id:
                    entry["id"] = delta.id
                if delta.name:
                    entry["name"] = delta.name
                if delta.arguments_fragment:
                    entry["arguments"] += delta.arguments_fragment
            elif isinstance(delta, UsageDelta):
                usage = delta.usage
            elif isinstance(delta, FinishDelta):
                finish_reason = delta.finish_reason
                model = delta.model or request.model

        calls: list[ToolCall] | None = None
        if tool_calls:
            calls = []
            for index in sorted(tool_calls):
                entry = tool_calls[index]
                try:
                    arguments = json.loads(entry.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": entry.get("arguments", "")}
                calls.append(
                    ToolCall(
                        id=entry.get("id") or f"call_{index}",
                        name=entry.get("name", ""),
                        arguments=arguments,
                    )
                )
        return ModelResponse(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=calls),
            usage=usage,
            finish_reason=finish_reason,
            model=model,
        )


def _backoff_delay(attempt: int, error: ModelError) -> float:
    retry_after: float | None = getattr(error, "retry_after", None)
    if retry_after is not None:
        return retry_after
    exponent: int = attempt - 1
    delay: float = BACKOFF_BASE_SECONDS * float(2**exponent)
    return delay


__all__ = ["FunctionCallingStrategy"]
