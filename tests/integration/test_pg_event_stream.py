"""PgEventStream integration (S1, ADR 0008 §4): DB tail + NOTIFY wake-ups,
exactly-once delivery by cursor, fallback poll when NOTIFY is missed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from jarvis.domain.events import RunCompleted, RunStarted
from jarvis.domain.execution import RunResult
from jarvis.events.bus import InProcessEventSink
from jarvis.events.pg_notify import PgEventStream, PgNotifier

pytestmark = pytest.mark.db


def _sink(container, run_id: str, *, notify: bool) -> InProcessEventSink:
    async def persist(event) -> int:
        cursor = await container.executions.append_event(event)
        if notify:
            await container.notifier.notify(event.run_id, cursor)
        return cursor

    return InProcessEventSink(run_id, persist=persist)


def _started(run_id: str, sequence: int | None = None) -> RunStarted:
    return RunStarted(
        event_id=f"e-{run_id}-{sequence}",
        run_id=run_id,
        created_at=datetime.now(UTC),
        agent_id="a",
        agent_version_id="v",
        sequence=sequence,
    )


def _completed(run_id: str) -> RunCompleted:
    return RunCompleted(
        event_id=f"t-{run_id}",
        run_id=run_id,
        created_at=datetime.now(UTC),
        final_message="done",
        total_usage=Usage(),
        iterations=1,
    )


from jarvis.domain.message import Usage  # noqa: E402


async def _collect(
    stream: PgEventStream, run_id: str, last_cursor: int | None, done: asyncio.Event
) -> list[tuple[int, str]]:
    seen: list[tuple[int, str]] = []
    async for cursor, event in stream.subscribe(run_id, last_cursor):
        seen.append((cursor, event.type))
        if event.type == "run.completed":
            break
    done.set()
    return seen


@pytest.mark.db
async def test_replay_then_live_then_terminal(container):
    run_id = "stream-replay-live"
    sink = _sink(container, run_id, notify=True)
    await sink.append(_started(run_id))

    stream = container.streams
    done = asyncio.Event()
    task = asyncio.create_task(_collect(stream, run_id, None, done))
    await asyncio.sleep(0.1)  # let the subscriber register + replay the prefix

    await sink.append(_started(run_id, 1))
    await sink.finalize(_completed(run_id))
    await asyncio.wait_for(done.wait(), timeout=5)

    seen = task.result()
    types = [t for _, t in seen]
    assert types == ["run.started", "run.started", "run.completed"]
    cursors = [c for c, _ in seen]
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)


@pytest.mark.db
async def test_resume_yields_only_events_after_last_cursor(container):
    run_id = "stream-resume"
    sink = _sink(container, run_id, notify=True)
    cursor_1 = await sink.append(_started(run_id))
    await sink.append(_started(run_id, 1))
    await sink.finalize(_completed(run_id))

    stream = container.streams
    done = asyncio.Event()
    task = asyncio.create_task(_collect(stream, run_id, cursor_1, done))
    await asyncio.wait_for(done.wait(), timeout=5)

    seen = task.result()
    cursors = [c for c, _ in seen]
    assert all(c > cursor_1 for c in cursors)
    assert [t for _, t in seen] == ["run.started", "run.completed"]


@pytest.mark.db
async def test_resume_at_terminal_cursor_ends_immediately(container):
    """The client consumed the run's terminal event before disconnecting:
    the reconnect's replay is empty, and the stream must end at once — not
    poll forever for events that will never come."""
    run_id = "stream-resume-at-terminal"
    sink = _sink(container, run_id, notify=True)
    await sink.append(_started(run_id))
    terminal_cursor = await sink.finalize(_completed(run_id))
    # A run row always exists when a route subscribes (404 happens first) —
    # the empty-batch check reads it to tell "finished" from "nothing new".
    await container.executions.create_run(
        RunResult(
            run_id=run_id,
            agent_id="a",
            status="succeeded",
            finished_at=datetime.now(UTC),
            event_cursor=terminal_cursor,
        )
    )

    stream = container.streams
    seen = [
        (cursor, event.type) async for cursor, event in stream.subscribe(run_id, terminal_cursor)
    ]
    assert seen == []  # nothing was missed — the empty stream is the answer


@pytest.mark.db
async def test_missed_notify_still_delivers_via_fallback_poll(container):
    run_id = "stream-missed-notify"
    sink = _sink(container, run_id, notify=False)  # persist WITHOUT pg_notify
    await sink.append(_started(run_id))

    stream = container.streams
    done = asyncio.Event()
    task = asyncio.create_task(_collect(stream, run_id, None, done))
    await asyncio.sleep(0.1)

    await sink.append(_started(run_id, 1))
    await sink.finalize(_completed(run_id))
    # No notify was ever sent — the 1s fallback poll must still deliver.
    await asyncio.wait_for(done.wait(), timeout=6)

    seen = task.result()
    assert [t for _, t in seen] == ["run.started", "run.started", "run.completed"]


@pytest.mark.db
async def test_replay_port_yields_events_only(container):
    run_id = "stream-replay-only"
    sink = _sink(container, run_id, notify=True)
    await sink.append(_started(run_id))
    await sink.finalize(_completed(run_id))

    stream = container.streams
    events = [event async for event in stream.replay(run_id)]
    assert [event.type for event in events] == ["run.started", "run.completed"]


@pytest.mark.db
async def test_notifier_is_best_effort_without_listeners(container):
    notifier = PgNotifier(container.settings.database_url)
    await notifier.notify("nobody-listens", 1)  # must not raise
    await notifier.aclose()


@pytest.mark.db
async def test_stream_ends_at_pause_segment(container):
    """S10: a run.awaiting_input frame ends the stream exactly like a
    terminal — the client shows the pause UI and re-attaches with
    Last-Event-ID after resume; a reconnect whose replay is empty ends at
    once because the row says awaiting_input."""
    from datetime import timedelta

    from jarvis.domain.events import RunAwaitingInput
    from jarvis.events.bus import EventSequenceError

    run_id = "stream-pause"
    sink = _sink(container, run_id, notify=True)
    await sink.append(_started(run_id))
    pause_cursor = await sink.append(
        RunAwaitingInput(
            event_id=f"p-{run_id}",
            run_id=run_id,
            created_at=datetime.now(UTC),
            reason="strategy",
            question="go on?",
            awaiting_until=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    # The paused row (what the empty-replay reconnect reads).
    await container.executions.create_run(
        RunResult(run_id=run_id, agent_id="a", status="awaiting_input")
    )

    stream = container.streams
    seen = [(c, e.type) async for c, e in stream.subscribe(run_id)]
    assert [t for _, t in seen] == ["run.started", "run.awaiting_input"]

    # The client consumed the pause frame; the reconnect's replay is empty
    # and the row is awaiting_input — the stream ends, it does not poll.
    seen = [(c, e.type) async for c, e in stream.subscribe(run_id, pause_cursor)]
    assert seen == []

    # The paused sink refuses further appends: its segment has ended.
    with pytest.raises(EventSequenceError, match="segment at a pause"):
        await sink.append(_started(run_id, 2))
