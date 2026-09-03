"""SSE framing: wire format + Last-Event-ID parsing (no DB, no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jarvis.api.sse import PING, SSE_HEADERS, frame, parse_last_event_id
from jarvis.domain.events import RunStarted


def _event() -> RunStarted:
    return RunStarted(
        event_id="evt-1",
        run_id="run-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        agent_id="agent-1",
        agent_version_id="ver-1",
        session_id="sess-1",
        input="hello",
        sequence=3,
    )


def test_frame_has_id_event_data_and_blank_line() -> None:
    text = frame(42, _event())
    lines = text.split("\n")
    assert lines[0] == "id: 42"
    assert lines[1] == "event: run.started"
    assert lines[-1] == ""  # frame ends with a blank line (SSE terminator)
    data = json.loads(lines[2].removeprefix("data: "))
    assert data["type"] == "run.started"
    assert data["sequence"] == 3
    assert data["input"] == "hello"


def test_frame_is_full_event_payload() -> None:
    event = _event()
    text = frame(7, event)
    data = json.loads(text.split("data: ")[1].strip())
    assert data == event.model_dump(mode="json")


def test_parse_last_event_id() -> None:
    assert parse_last_event_id(None) is None
    assert parse_last_event_id("17") == 17
    with pytest.raises(ValueError):
        parse_last_event_id("not-a-number")


def test_ping_and_headers() -> None:
    assert PING.startswith(":")
    assert SSE_HEADERS["Cache-Control"] == "no-store"
