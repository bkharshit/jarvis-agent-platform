"""Domain event envelope invariants (commit 2 core guarantee)."""

import pytest
from pydantic import ValidationError

from jarvis.domain.events import (
    EventSequenceError,
    IterationCompleted,
    IterationStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolCallCompleted,
    is_terminal,
    validate_event_sequence,
)
from jarvis.domain.message import Usage


def _event(cls, sequence: int, **kwargs):
    return cls(event_id=f"e{sequence}", run_id="r1", sequence=sequence, **kwargs)


def _usage() -> Usage:
    return Usage(input_tokens=1, output_tokens=2)


def test_valid_sequence_passes():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(IterationStarted, 1, iteration=0),
        _event(TextDelta, 2, text="hi"),
        _event(IterationCompleted, 3, iteration=0, usage=_usage()),
        _event(RunCompleted, 4, final_message="hi", total_usage=_usage(), iterations=1),
    ]
    validate_event_sequence(events)  # no raise


def test_gap_in_sequence_raises():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(TextDelta, 2, text="gap!"),
        _event(RunCompleted, 3, final_message="x", total_usage=_usage(), iterations=1),
    ]
    with pytest.raises(EventSequenceError, match="gapless"):
        validate_event_sequence(events)


def test_sequence_zero_gapless_required():
    events = [
        _event(RunStarted, 1, agent_id="a1", agent_version_id="v1"),
        _event(RunCompleted, 2, final_message="x", total_usage=_usage(), iterations=1),
    ]
    with pytest.raises(EventSequenceError, match="gapless"):
        validate_event_sequence(events)


def test_two_terminal_events_raise():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(RunCompleted, 1, final_message="x", total_usage=_usage(), iterations=1),
        _event(RunFailed, 2, error="boom", error_kind="model", total_usage=_usage()),
    ]
    with pytest.raises(EventSequenceError, match="more than one terminal"):
        validate_event_sequence(events)


def test_event_after_terminal_raises():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(RunCompleted, 1, final_message="x", total_usage=_usage(), iterations=1),
        _event(TextDelta, 2, text="too late"),
    ]
    with pytest.raises(EventSequenceError, match="after terminal"):
        validate_event_sequence(events)


def test_missing_terminal_raises():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(TextDelta, 1, text="no end"),
    ]
    with pytest.raises(EventSequenceError, match="no terminal event"):
        validate_event_sequence(events)


def test_empty_sequence_is_valid():
    validate_event_sequence([])


def test_mixed_run_ids_raise():
    events = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        RunCancelled(event_id="x", run_id="other", sequence=1, reason="stop", total_usage=_usage()),
    ]
    with pytest.raises(EventSequenceError, match="run_id"):
        validate_event_sequence(events)


def test_is_terminal():
    started = _event(RunStarted, 0, agent_id="a1", agent_version_id="v1")
    completed = _event(RunCompleted, 1, final_message="x", total_usage=_usage(), iterations=1)
    failed = _event(RunFailed, 1, error="x", error_kind="model", total_usage=_usage())
    cancelled = _event(RunCancelled, 1, reason="x", total_usage=_usage())
    delta = _event(TextDelta, 1, text="x")
    assert not is_terminal(started)
    assert not is_terminal(delta)
    assert is_terminal(completed)
    assert is_terminal(failed)
    assert is_terminal(cancelled)


def test_events_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        TextDelta(event_id="e", run_id="r", sequence=0, text="x", surprise=True)


def test_run_failed_rejects_unknown_error_kind():
    with pytest.raises(ValidationError):
        RunFailed(
            event_id="e",
            run_id="r",
            sequence=0,
            error="x",
            error_kind="weird",
            total_usage=Usage(),
        )


def test_tool_call_completed_roundtrip():
    event = _event(ToolCallCompleted, 5, tool_call_id="tc1", name="calculator", output="42")
    assert event.sequence == 5
    assert event.type == "tool.call.completed"
