"""SSE framing — a thin transport adapter, framing only (plan §5).

One frame per event: `id: <cursor>` (the durable `execution_events.cursor` —
the SSE Last-Event-ID, ADR 0003), `event: <type>`, `data: <event JSON>`.
The stream route owns subscription/replay; this module owns the wire format."""

from __future__ import annotations

import json

from jarvis.domain.events import ExecutionEvent

PING = ": keep-alive\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def frame(cursor: int, event: ExecutionEvent) -> str:
    payload = event.model_dump(mode="json")
    return f"id: {cursor}\nevent: {event.type}\ndata: {json.dumps(payload)}\n\n"


def parse_last_event_id(raw: str | None) -> int | None:
    """Header -> cursor. Raises ValueError on a malformed id rather than
    silently replaying from zero (exactly-once matters)."""
    if raw is None:
        return None
    return int(raw)


__all__ = ["PING", "SSE_HEADERS", "frame", "parse_last_event_id"]
