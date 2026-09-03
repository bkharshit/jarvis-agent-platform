"""Declared provider capabilities (ADR 0005).

A provider lacking a capability is representable up front — never discovered
mid-run. `structured_output` tiers: `none` (prompt-only), `json_mode`
(e.g. Ollama), `json_schema` (native)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

StructuredOutputMode = Literal["none", "json_mode", "json_schema"]


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool = True
    function_calling: bool = True
    structured_output: StructuredOutputMode = "json_schema"
    parallel_tool_calls: bool = False


__all__ = ["ModelCapabilities", "StructuredOutputMode"]
