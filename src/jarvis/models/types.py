"""Model layer types: requests, responses, stream deltas.

ADR 0005: generate and stream are separate; the stream is a typed union of
deltas (no overloaded return types).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolDescriptor


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRequest(_Model):
    model: str
    messages: list[Message]
    tools: list[ToolDescriptor] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, object] | None = None
    stop: list[str] | None = None


class ModelResponse(_Model):
    message: Message  # assistant; may carry tool_calls
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""


class TextDelta(_Model):
    type: Literal["text"] = "text"
    text: str


class ToolCallDelta(_Model):
    type: Literal["tool_call"] = "tool_call"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str = ""


class UsageDelta(_Model):
    type: Literal["usage"] = "usage"
    usage: Usage


class FinishDelta(_Model):
    type: Literal["finish"] = "finish"
    finish_reason: str = "stop"
    model: str = ""


StreamDelta = Annotated[
    TextDelta | ToolCallDelta | UsageDelta | FinishDelta,
    Field(discriminator="type"),
]

__all__ = [
    "FinishDelta",
    "ModelRequest",
    "ModelResponse",
    "StreamDelta",
    "TextDelta",
    "ToolCallDelta",
    "UsageDelta",
    "ToolCall",
]
