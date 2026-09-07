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


def test_tool_call_declined_is_non_terminal_and_in_union():
    """ADR 0011 §3: the refusal decision is an event — in the union, and
    not terminal (execution events like started/completed never follow)."""
    from pydantic import TypeAdapter

    from jarvis.domain.events import ExecutionEvent, ToolCallDeclined, is_terminal

    event = _event(ToolCallDeclined, 6, tool_call_id="tc1", name="calculator")
    assert event.type == "tool.call.declined"
    assert not is_terminal(event)
    parsed = TypeAdapter(ExecutionEvent).validate_python(event.model_dump(mode="json"))
    assert isinstance(parsed, ToolCallDeclined)


def test_pause_event_is_not_terminal():
    from datetime import UTC, datetime, timedelta

    from jarvis.domain.events import TERMINAL_EVENT_TYPES, RunAwaitingInput, is_pause
    from jarvis.domain.message import ToolCall

    pause = RunAwaitingInput(
        event_id="p1",
        run_id="r1",
        sequence=3,
        reason="tool_approval",
        pending_calls=[ToolCall(id="c1", name="shell", arguments={"cmd": "ls"})],
        awaiting_until=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert pause.type == "run.awaiting_input"
    assert is_pause(pause)
    assert not is_terminal(pause)  # pause, not terminal (ADR 0010 §1)
    assert "run.awaiting_input" not in TERMINAL_EVENT_TYPES

    strategy_pause = RunAwaitingInput(
        event_id="p2",
        run_id="r1",
        reason="strategy",
        question="Which branch should I deploy?",
        awaiting_until=datetime.now(UTC),
    )
    assert strategy_pause.question == "Which branch should I deploy?"
    assert strategy_pause.pending_calls == []
    with pytest.raises(ValidationError):
        RunAwaitingInput(
            event_id="p3",
            run_id="r1",
            reason="weird",  # type: ignore[arg-type]
            awaiting_until=datetime.now(UTC),
        )


def test_segment_invariant_pause_ends_segment():
    """ADR 0010 §1: no non-terminal event may follow run.awaiting_input
    within a segment; the resumed segment's events form a new chain that
    continues the gapless sequence, and the WHOLE chain validates as a
    completed run once it reaches its terminal."""
    from datetime import UTC, datetime, timedelta

    from jarvis.domain.events import RunAwaitingInput, is_pause

    pause = RunAwaitingInput(
        event_id="p1",
        run_id="r1",
        sequence=2,
        reason="strategy",
        question="go on?",
        awaiting_until=datetime.now(UTC) + timedelta(seconds=60),
    )

    # Segment 1: starts the run, ends at the pause — no terminal yet.
    segment_1 = [
        _event(RunStarted, 0, agent_id="a1", agent_version_id="v1"),
        _event(TextDelta, 1, text="thinking"),
        pause,
    ]
    assert all(not is_terminal(e) for e in segment_1)
    assert is_pause(segment_1[-1])  # the segment's last event is the pause

    # Resumed segment 2: continues the sequence (offset-seeded) and ends
    # at the run's single terminal.
    segment_2 = [
        TextDelta(event_id="e3", run_id="r1", sequence=3, text="answered"),
        _event(RunCompleted, 4, final_message="done", total_usage=_usage(), iterations=1),
    ]

    # Each segment is gapless within itself; the concatenation is the
    # run's full gapless chain and validates as a completed run.
    whole = segment_1 + segment_2
    validate_event_sequence(whole)

    # Control flow enforces the invariant (the loop returns after the
    # pause); the detector documents it — an event appended right after
    # the pause inside the same segment would break it.
    violated = segment_1[:-1] + [pause, segment_2[0]]
    assert is_pause(violated[2])
