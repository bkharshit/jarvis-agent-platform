"""Minimal ReAct strategy — Thought/Action/Action Input/Final Answer.

Uses plain-text generation (no native function calling). The prompt engine
renders the tool section and format instructions into the system prompt for
`strategy.type == "react"`. Malformed replies get one corrective developer
message per step instead of a crash; the orchestrator's max-iteration cap is
the structural backstop."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jarvis.domain.events import ModelInvocationCompleted, ModelInvocationStarted
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.errors import RETRYABLE_ERRORS, ModelError
from jarvis.models.types import ModelRequest, ModelResponse
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient
from jarvis.ports.strategy import FinishStep, StepOutcome, ToolCallsStep

MAX_ATTEMPTS = 2

_ACTION_PATTERN = re.compile(r"Action:\s*(\S+)\s*\n", re.IGNORECASE)
_INPUT_PATTERN = re.compile(r"Action Input:\s*(\{.*?\})", re.DOTALL)
_FINAL_PATTERN = re.compile(r"Final Answer:\s*(.*)", re.DOTALL)


class ReActStrategy:
    @property
    def name(self) -> str:
        return "react"

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
            tools=None,  # ReAct never sends native tool schemas
            temperature=ctx.temperature,
        )
        response = await self._generate_with_retry(ctx, request, client, sink)
        assistant = response.message
        text = assistant.text

        final = _FINAL_PATTERN.search(text)
        if final:
            # The user-facing answer is what follows the marker, not the
            # raw protocol line — strip it from the finish message.
            return FinishStep(
                assistant_message=assistant.model_copy(update={"content": final.group(1)}),
                finish_reason="stop",
            )

        action = _ACTION_PATTERN.search(text)
        action_input = _INPUT_PATTERN.search(text)
        if action:
            tool_name = action.group(1).strip()
            arguments = _parse_action_input(action_input)
            tool_names = {tool.name for tool in tools}
            if tool_name not in tool_names:
                return self._recovery_step(
                    assistant, f"Action {tool_name!r} is not an available tool."
                )
            return ToolCallsStep(
                assistant_message=assistant,
                tool_calls=[
                    ToolCall(
                        id=f"react-{ctx.run_id[:8]}-{ctx.iteration}",
                        name=tool_name,
                        arguments=arguments,
                    )
                ],
            )

        return self._recovery_step(assistant, "no 'Action:' or 'Final Answer:' found in the reply.")

    def _recovery_step(self, assistant: Message, problem: str) -> StepOutcome:
        """Malformed action recovery: no crash, no loop — ask the model to
        retry in the correct format on the next iteration."""
        from jarvis.prompt.engine import REACT_FORMAT_INSTRUCTIONS

        correction = Message(
            role="developer",
            content=(
                f"Your previous reply could not be used ({problem}). "
                f"Reply again using exactly the expected format.\n\n{REACT_FORMAT_INSTRUCTIONS}"
            ),
        )
        return ToolCallsStep(
            assistant_message=assistant,
            tool_calls=[],
            messages=[correction],
        )

    async def _generate_with_retry(
        self,
        ctx: ExecutionContext,
        request: ModelRequest,
        client: ModelClient,
        sink: EventSink,
    ) -> ModelResponse:
        last_error: ModelError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await sink.append(
                ModelInvocationStarted(
                    event_id=str(uuid4()),
                    run_id=ctx.run_id,
                    attempt=attempt,
                    created_at=datetime.now(UTC),
                )
            )
            try:
                response = await client.generate(request, cancel=ctx.cancel)
                ctx.usage = ctx.usage.plus(response.usage)
            except RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
                raise
            await sink.append(
                ModelInvocationCompleted(
                    event_id=str(uuid4()),
                    run_id=ctx.run_id,
                    usage=response.usage,
                    finish_reason=response.finish_reason,
                    model=response.model,
                    created_at=datetime.now(UTC),
                )
            )
            return response
        assert last_error is not None
        raise last_error


def _parse_action_input(match: re.Match[str] | None) -> dict[str, Any]:
    if match is None:
        return {}
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"_raw": match.group(1)}
    return parsed if isinstance(parsed, dict) else {"_raw": match.group(1)}


__all__ = ["ReActStrategy"]
