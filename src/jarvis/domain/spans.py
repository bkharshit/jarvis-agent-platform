"""Span domain types (S7, ADR 0021 §1/§3, D71) — pure Pydantic, no IO.

A `SpanRecord` is one FINISHED span (write-on-end, D74): the runtime
builds them around boundaries that already exist (segment, iteration,
model call, tool call, retrieval) and flushes buffered records to a
`SpanSink`. The OTel trace_id IS the run's persisted `trace_id`
(D73 — `uuid4().hex` is 32 hex = a valid 128-bit OTel trace id
verbatim), so a run and its children are one trace by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SpanKind = Literal["run.segment", "iteration", "llm.call", "tool.call", "knowledge.retrieve"]
SpanStatus = Literal["ok", "error", "cancelled"]


class SpanRecord(BaseModel):
    """One finished span. `attributes` is open — spans carry event-cursor
    ties (`jarvis.event.cursor`), run linkage (`parent_run_id`), and
    whatever the seam already had in hand (tool name, usage, finish
    reason). Never secret material: credentials ride HTTP headers, never
    the runtime's objects (ADR 0005/0006)."""

    model_config = ConfigDict(extra="forbid")

    span_id: str = Field(  # 16 hex — an OTel span id
        default_factory=lambda: uuid4().hex[:16], pattern=r"^[0-9a-f]{16}$"
    )
    trace_id: str = Field(  # the run's trace_id (32 hex), never minted here
        pattern=r"^[0-9a-f]{32}$"
    )
    run_id: str
    tenant_id: str | None = None  # the run's tenant; the sink scopes by it
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    kind: SpanKind
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float = 0.0
    status: SpanStatus = "ok"
    status_message: str | None = None
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


__all__ = ["SpanKind", "SpanRecord", "SpanStatus"]
