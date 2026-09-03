"""MockModelProvider — scripted turns, failure injection, request recording.

Deterministic full tool loops with zero network: tests and CLI demo use
`provider: "mock"`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.execution import CancellationToken
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.errors import ModelAbortedError
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    StreamDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)


class MockTurn(BaseModel):
    """One scripted model invocation. Exactly one of content/tool_calls is
    meaningful per call; `error` injects a failure; `deltas` overrides the
    synthesized stream."""

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    deltas: list[StreamDelta] | None = None
    error: Any = None  # exception instance or class to raise
    usage: Usage = Field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))
    finish_reason: str = "stop"


def turn(content: str = "", *, tool_calls: list[ToolCall] | None = None, **kw: Any) -> MockTurn:
    return MockTurn(content=content, tool_calls=tool_calls or [], **kw)


class MockModelProvider:
    """Implements the ModelProvider protocol. Records every request."""

    def __init__(
        self,
        turns: list[MockTurn] | None = None,
        *,
        default_content: str = "This is a mock response.",
        name: str = "mock",
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self.default_content = default_content
        self._name = name
        self.capabilities = capabilities or ModelCapabilities()
        self.requests: list[ModelRequest] = []
        self.invocations = 0

    @property
    def name(self) -> str:
        return self._name

    def add_turn(self, scripted: MockTurn) -> None:
        self._turns.append(scripted)

    def _next_turn(self, request: ModelRequest) -> MockTurn:
        self.requests.append(request)
        self.invocations += 1
        if self._turns:
            return self._turns.pop(0)
        return MockTurn(content=self.default_content)

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        if cancel is not None and cancel.triggered:
            raise ModelAbortedError("cancelled before invocation", provider=self.name)
        scripted = self._next_turn(request)
        if scripted.error is not None:
            raise scripted.error
        return ModelResponse(
            message=self._assistant_message(scripted),
            usage=scripted.usage,
            finish_reason=scripted.finish_reason,
            model=request.model,
        )

    async def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        if cancel is not None and cancel.triggered:
            raise ModelAbortedError("cancelled before invocation", provider=self.name)
        scripted = self._next_turn(request)
        if scripted.error is not None:
            raise scripted.error
        if scripted.deltas is not None:
            for delta in scripted.deltas:
                if cancel is not None and cancel.triggered:
                    raise ModelAbortedError("cancelled mid-stream", provider=self.name)
                yield delta
            return
        if scripted.content:
            for fragment in scripted.content.split(" "):
                if cancel is not None and cancel.triggered:
                    raise ModelAbortedError("cancelled mid-stream", provider=self.name)
                yield TextDelta(text=fragment + " ")
        for index, call in enumerate(scripted.tool_calls):
            yield ToolCallDelta(index=index, id=call.id, name=call.name)
            yield ToolCallDelta(index=index, arguments_fragment=_json_arguments(call))
        yield UsageDelta(usage=scripted.usage)
        yield FinishDelta(finish_reason=scripted.finish_reason, model=request.model)

    @staticmethod
    def _assistant_message(scripted: MockTurn) -> Message:
        tool_calls = scripted.tool_calls or None
        return Message(role="assistant", content=scripted.content, tool_calls=tool_calls)


def _json_arguments(call: ToolCall) -> str:
    import json

    return json.dumps(call.arguments)


__all__ = ["MockModelProvider", "MockTurn", "turn"]
