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
    EmbeddingRequest,
    EmbeddingResponse,
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
        models: list[str] | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self.default_content = default_content
        self._name = name
        self.capabilities = capabilities or ModelCapabilities()
        self.models = list(models) if models is not None else ["mock-small", "mock-large"]
        self.requests: list[ModelRequest] = []
        self.invocations = 0

    @property
    def name(self) -> str:
        return self._name

    async def list_models(self) -> list[str]:
        return list(self.models)

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
            fragments = scripted.content.split(" ")
            for index, fragment in enumerate(fragments):
                if cancel is not None and cancel.triggered:
                    raise ModelAbortedError("cancelled mid-stream", provider=self.name)
                # separator goes on the front so the reconstruction has no trailing space
                yield TextDelta(text=fragment if index == 0 else " " + fragment)
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


class MockEmbeddingProvider:
    """Deterministic zero-network EmbeddingClient (S8, ADR 0020 §2, D64).

    The vector is a NORMALIZED HASHED BAG OF WORDS, not a plain hash —
    identical text embeds identically AND cosine similarity is meaningful
    (texts sharing tokens share buckets), so retrieval ranking is real in
    tests. Records every request; `errors` pops one per embed call."""

    def __init__(
        self,
        *,
        dimension: int = 8,
        errors: list[Any] | None = None,
        name: str = "mock",
    ) -> None:
        self._dimension = dimension
        self._errors = list(errors or [])
        self._name = name
        self.requests: list[EmbeddingRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def list_models(self) -> list[str]:
        return ["mock-embedder"]

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.requests.append(request)
        if self._errors:
            scripted = self._errors.pop(0)
            raise scripted() if isinstance(scripted, type) else scripted
        return EmbeddingResponse(
            vectors=[mock_embedding(text, self._dimension) for text in request.input]
        )


def mock_embedding(text: str, dimension: int) -> list[float]:
    """Normalized hashed bag of words: each token hashes to one bucket;
    shared tokens → shared buckets → real cosine ranking."""
    import hashlib
    import math
    import re

    vector = [0.0] * dimension
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


__all__ = ["MockEmbeddingProvider", "MockModelProvider", "MockTurn", "mock_embedding", "turn"]
