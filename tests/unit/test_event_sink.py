"""InProcessEventSink invariants: gapless sequence, terminal semantics,
replay + live subscription with exactly-once resume."""

import asyncio
from uuid import uuid4

import pytest

from jarvis.domain.events import (
    EventSequenceError,
    RunCompleted,
    TextDelta,
    validate_event_sequence,
)
from jarvis.domain.message import Usage
from jarvis.events.bus import InProcessEventBus, InProcessEventSink


def _sink() -> InProcessEventSink:
    return InProcessEventSink("r1")


def _delta(text: str) -> TextDelta:
    return TextDelta(event_id=str(uuid4()), run_id="r1", text=text)


def _terminal() -> RunCompleted:
    return RunCompleted(
        event_id=str(uuid4()),
        run_id="r1",
        final_message="done",
        total_usage=Usage(),
        iterations=1,
    )


class TestAppend:
    async def test_sequence_is_gapless_from_zero(self):
        sink = _sink()
        assert await sink.append(_delta("a")) == 0
        assert await sink.append(_delta("b")) == 1
        assert [e.sequence for e in sink.events] == [0, 1]

    async def test_wrong_run_id_rejected(self):
        sink = _sink()
        event = TextDelta(event_id=str(uuid4()), run_id="other", text="x")
        with pytest.raises(EventSequenceError, match="does not match"):
            await sink.append(event)

    async def test_preset_wrong_sequence_rejected(self):
        sink = _sink()
        event = _delta("x")
        event.sequence = 5
        with pytest.raises(EventSequenceError, match="sequence"):
            await sink.append(event)

    async def test_terminal_via_append_rejected(self):
        sink = _sink()
        with pytest.raises(EventSequenceError, match="finalize"):
            await sink.append(_terminal())


class TestFinalize:
    async def test_finalize_appends_and_closes(self):
        sink = _sink()
        await sink.append(_delta("a"))
        cursor = await sink.finalize(_terminal())
        assert cursor == 1
        assert sink.finalized
        validate_event_sequence(sink.events)

    async def test_finalize_twice_raises(self):
        sink = _sink()
        await sink.finalize(_terminal())
        with pytest.raises(EventSequenceError, match="already finalized"):
            await sink.finalize(_terminal())

    async def test_append_after_finalize_raises(self):
        sink = _sink()
        await sink.finalize(_terminal())
        with pytest.raises(EventSequenceError, match="already finalized"):
            await sink.append(_delta("late"))

    async def test_finalize_rejects_non_terminal(self):
        sink = _sink()
        with pytest.raises(EventSequenceError, match="terminal events only"):
            await sink.finalize(_delta("nope"))


class TestSubscribe:
    async def test_live_subscription_receives_all_then_terminal(self):
        sink = _sink()
        received: list[str] = []

        async def _runner():
            await sink.append(_delta("a"))
            await sink.append(_delta("b"))
            await sink.finalize(_terminal())

        async def _subscriber():
            async for _cursor, event in sink.subscribe():
                received.append(event.type)
                if event.type == "run.completed":
                    break

        subscriber = asyncio.ensure_future(_subscriber())
        await asyncio.sleep(0.01)
        await _runner()
        await subscriber
        assert received == ["text.delta", "text.delta", "run.completed"]

    async def test_resume_after_cursor_is_exactly_once(self):
        sink = _sink()
        await sink.append(_delta("a"))  # cursor 0
        await sink.append(_delta("b"))  # cursor 1

        # subscriber joins late, having seen cursor 0
        received: list[str] = []

        async def _subscriber():
            async for _cursor, event in sink.subscribe(last_cursor=0):
                received.append(event.type)
                if event.type == "run.completed":
                    break

        subscriber = asyncio.ensure_future(_subscriber())
        await asyncio.sleep(0.01)
        await sink.append(_delta("c"))  # cursor 2 — live
        await sink.finalize(_terminal())  # cursor 3
        await subscriber
        # 'a' (cursor 0 <= last) skipped; replay of 'b' exactly once; then live
        assert received == ["text.delta", "text.delta", "run.completed"]

    async def test_replay_of_finished_run_ends_at_terminal(self):
        sink = _sink()
        await sink.append(_delta("a"))
        await sink.finalize(_terminal())
        received = [event.type async for _c, event in sink.subscribe()]
        assert received == ["text.delta", "run.completed"]

    async def test_subscribe_late_with_full_history(self):
        sink = _sink()
        await sink.append(_delta("a"))
        await sink.append(_delta("b"))
        await sink.finalize(_terminal())
        received = [e.type async for _c, e in sink.subscribe(last_cursor=1)]
        assert received == ["run.completed"]


class TestBus:
    async def test_get_or_create_is_idempotent(self):
        bus = InProcessEventBus()
        first = await bus.get_or_create("r1")
        second = await bus.get_or_create("r1")
        assert first is second

    async def test_active_runs_and_drop(self):
        bus = InProcessEventBus()
        sink = await bus.get_or_create("r1")
        await sink.finalize(_terminal())
        assert bus.active_runs() == []
        bus.drop("r1")
        assert bus.get("r1") is None


def _pause():
    from datetime import UTC, datetime, timedelta

    from jarvis.domain.events import RunAwaitingInput

    return RunAwaitingInput(
        event_id=str(uuid4()),
        run_id="r1",
        reason="strategy",
        question="go on?",
        awaiting_until=datetime.now(UTC) + timedelta(seconds=60),
    )


class TestSequenceOffset:
    async def test_offset_seeds_a_resumed_segment(self):
        """A fresh sink for an existing log continues the gapless sequence
        (S10) — append_event trusts the sink-assigned sequence, so a
        restart-at-0 would collide."""
        sink = InProcessEventSink("r1", sequence_offset=3)
        assert await sink.append(_delta("a")) == 3
        assert await sink.append(_delta("b")) == 4
        assert [e.sequence for e in sink.events] == [3, 4]

    async def test_offset_zero_unchanged(self):
        sink = _sink()
        assert await sink.append(_delta("a")) == 0


class TestPauseSegment:
    async def test_pause_ends_the_segment(self):
        sink = _sink()
        await sink.append(_delta("a"))
        cursor = await sink.append(_pause())
        assert cursor == 1
        assert sink.paused and not sink.finalized

    async def test_append_after_pause_raises(self):
        sink = _sink()
        await sink.append(_pause())
        with pytest.raises(EventSequenceError, match="segment at a pause"):
            await sink.append(_delta("late"))

    async def test_finalize_after_pause_raises(self):
        sink = _sink()
        await sink.append(_pause())
        with pytest.raises(EventSequenceError, match="segment at a pause"):
            await sink.finalize(_terminal())

    async def test_subscribe_ends_at_pause(self):
        sink = _sink()
        await sink.append(_delta("a"))
        await sink.append(_pause())
        received = [event.type async for _c, event in sink.subscribe()]
        assert received == ["text.delta", "run.awaiting_input"]

    async def test_subscribe_ends_at_pause_live(self):
        sink = _sink()
        received: list[str] = []

        async def _runner():
            await sink.append(_delta("a"))
            await sink.append(_pause())

        async def _subscriber():
            async for _cursor, event in sink.subscribe():
                received.append(event.type)
                if event.type == "run.awaiting_input":
                    break

        subscriber = asyncio.ensure_future(_subscriber())
        await asyncio.sleep(0.01)
        await _runner()
        await subscriber
        assert received == ["text.delta", "run.awaiting_input"]

    async def test_active_runs_excludes_paused(self):
        bus = InProcessEventBus()
        sink = await bus.get_or_create("r1")
        await sink.append(_delta("a"))
        assert bus.active_runs() == ["r1"]
        await sink.append(_pause())
        assert bus.active_runs() == []  # paused, not active
