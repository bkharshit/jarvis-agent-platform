"""Message primitives — pure Pydantic, no IO."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Role = Literal["system", "developer", "user", "assistant", "tool"]


class TextPart(_Model):
    type: Literal["text"] = "text"
    text: str


class ImagePart(_Model):
    """Reserved for multimodal phases; not produced by Phase 1 code."""

    type: Literal["image"] = "image"
    url: str


ContentPart = Union[TextPart, ImagePart]


class ToolCall(_Model):
    id: str
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class Usage(_Model):
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, int] = Field(default_factory=dict)

    def plus(self, other: Usage) -> Usage:
        """Return the sum of this usage and ``other`` (new instance)."""
        extra = dict(self.extra)
        for key, value in other.extra.items():
            extra[key] = extra.get(key, 0) + value
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            extra=extra,
        )


class Message(_Model):
    role: Role
    content: str | list[ContentPart] = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def text(self) -> str:
        """Flattened text content (concatenated text parts / raw string)."""
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content if isinstance(part, TextPart))


def system(content: str) -> Message:
    return Message(role="system", content=content)


def developer(content: str) -> Message:
    return Message(role="developer", content=content)


def user(content: str) -> Message:
    return Message(role="user", content=content)


def assistant(
    content: str = "",
    tool_calls: list[ToolCall] | None = None,
) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls)


def tool_result(tool_call_id: str, name: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=tool_call_id, name=name)