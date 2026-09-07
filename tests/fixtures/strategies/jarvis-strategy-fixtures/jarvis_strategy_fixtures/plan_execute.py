"""PlanExecuteStrategy — the S3 sample plugin.

Plan-and-execute as transcript-driven phases, the same discipline ReAct
uses (stateless; per-run state lives in the transcript, never on `self`):

- Phase PLAN: no plan marker in any assistant message yet → one model
  invocation asking for a numbered plan, returned as a **think step**
  (empty `tool_calls` — the orchestrator persists the assistant message
  and continues the loop).
- Phase EXECUTE: one model invocation executing the next plan step. A
  reply containing the done marker ends the run (`FinishStep`); tool
  calls are handed back; plain text is a think step (the loop continues to
  the next plan step). Markers match as substrings, like the plan phase —
  real models do not reliably obey marker-placement instructions.

Markers come from `StrategyConfig.params` via
`ctx.metadata["strategy_params"]` (plan_marker / done_marker).
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
from jarvis.domain.message import developer as developer_message
from jarvis.domain.tools import ToolDescriptor
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

_PLAN_INSTRUCTION = (
    "You are the planner in a plan-and-execute loop. Reply with a short "
    "numbered plan (one action per line) for the user's request. Start your "
    "reply with the plan marker, then list the steps."
)
_EXECUTE_INSTRUCTION = (
    "You are the executor in a plan-and-execute loop. The transcript holds "
    "the plan and the steps already taken. Execute the NEXT unfinished plan "
    "step (using tools if appropriate). When every step is complete, start "
    "your reply with the done marker followed by a one-line summary."
)


class PlanExecuteStrategy:
    @property
    def name(self) -> str:
        return "plan_execute"

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[ToolDescriptor],
        sink: EventSink,
    ) -> StepOutcome:
        params: dict[str, Any] = ctx.metadata.get("strategy_params") or {}
        plan_marker = str(params.get("plan_marker", "PLAN:"))
        done_marker = str(params.get("done_marker", "DONE:"))

        assistant_texts = [m.text for m in messages if m.role == "assistant"]

        if not any(plan_marker in text for text in assistant_texts):
            # Phase PLAN — the planner instruction is appended (mirroring the
            # executor branch); the reply is a think step; the loop continues.
            response = await self._invoke(
                ctx,
                ModelRequest(
                    model="",
                    messages=[*messages, developer_message(_PLAN_INSTRUCTION)],
                    temperature=ctx.temperature,
                ),
                client,
                sink,
            )
            return ToolCallsStep(assistant_message=response.message)

        if done_marker in (assistant_texts[-1] if assistant_texts else ""):
            return FinishStep(assistant_message=messages[-1], finish_reason="stop")

        # Phase EXECUTE — tools ride along; a DONE reply ends the run.
        request = ModelRequest(
            model="",
            messages=[*messages, developer_message(_EXECUTE_INSTRUCTION)],
            tools=tools or None,
            temperature=ctx.temperature,
        )
        response = await self._invoke(ctx, request, client, sink)
        if done_marker in response.message.text:
            return FinishStep(assistant_message=response.message, finish_reason="stop")
        if response.message.tool_calls:
            return ToolCallsStep(
                assistant_message=response.message, tool_calls=response.message.tool_calls
            )
        return ToolCallsStep(assistant_message=response.message)  # think step

    async def _invoke(
        self,
        ctx: ExecutionContext,
        request: ModelRequest,
        client: ModelClient,
        sink: EventSink,
    ) -> ModelResponse:
        # One model invocation per step (ADR 0004); events through the sink
        # only — same shape the core strategies emit, without their retries.
        # Text streams as text.delta events when the provider supports it,
        # so blocking and SSE runs show the same transcript.
        await sink.append(
            ModelInvocationStarted(
                event_id=str(uuid4()), run_id=ctx.run_id, created_at=datetime.now(UTC)
            )
        )
        if client.capabilities.streaming:
            response = await self._invoke_streaming(ctx, request, client, sink)
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
        self,
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
