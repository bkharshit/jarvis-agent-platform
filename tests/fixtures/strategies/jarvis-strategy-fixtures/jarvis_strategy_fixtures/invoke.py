"""Shared model-invocation helper for the fixture strategies.

One model invocation per step (ADR 0004); events through the sink only —
the same discipline the core strategies use, without their retries. Text
streams as ``text.delta`` events when the provider supports streaming, so
the run console renders each phase's reply live.
"""

from __future__ import annotations

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


async def invoke(
    ctx: ExecutionContext,
    request: ModelRequest,
    client: ModelClient,
    sink: EventSink,
) -> ModelResponse:
    await sink.append(
        ModelInvocationStarted(
            event_id=str(uuid4()), run_id=ctx.run_id, created_at=datetime.now(UTC)
        )
    )
    if client.capabilities.streaming:
        response = await _invoke_streaming(ctx, request, client, sink)
    else:
        response = await client.generate(request, cancel=ctx.cancel)
    ctx.usage = ctx.usage.plus(response.usage)
    await sink.append(
        ModelInvocationCompleted(
            event_id=str(uuid4()),
            run_id=ctx.run_id,
            created_at=datetime.now(UTC),
            usage=response.usage,
            finish_reason=response.finish_reason,
            model=response.model,
        )
    )
    return response


async def _invoke_streaming(
    ctx: ExecutionContext,
    request: ModelRequest,
    client: ModelClient,
    sink: EventSink,
) -> ModelResponse:
    """Consume the delta stream: forward text to the sink, accumulate
    the reply — the same discipline function_calling uses, minus retries."""
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage = Usage()
    finish_reason = "stop"
    model = request.model
    async for delta in client.stream(request, cancel=ctx.cancel):
        if isinstance(delta, ModelTextDelta):
            text_parts.append(delta.text)
            await sink.append(
                TextDelta(
                    event_id=str(uuid4()),
                    run_id=ctx.run_id,
                    created_at=datetime.now(UTC),
                    text=delta.text,
                )
            )
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
