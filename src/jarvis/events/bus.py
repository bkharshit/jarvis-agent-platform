"""InProcessEventSink + bus — sequence assignment, live subscription, replay.

One sink instance per run. The sink is the ONLY place a per-run gapless
sequence is assigned (ADR 0003); when a persist callback is attached, the
returned cursor is the durable global `execution_events.cursor` (SSE
Last-Event-ID). The `EventSink`/`EventStream` port anticipates swapping this
for a queue-backed implementation without touching callers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from jarvis.domain.events import (
    EventSequenceError,
    ExecutionEvent,
    TerminalEvent,
    is_terminal,
)

# Persist callback: (event with sequence assigned) -> durable global cursor.
PersistFn = Callable[[ExecutionEvent], Awaitable[int]]


class InProcessEventSink:
    """Write/read side for one run. NOT thread-safe across event loops — a
    run and its subscribers live on the same asyncio loop."""

    def __init__(self, run_id: str, persist: PersistFn | None = None) -> None:
        self.run_id = run_id
        self._persist = persist
        self._events: list[ExecutionEvent] = []
        self._cursors: list[int] = []
        self._subscribers: list[asyncio.Queue[tuple[int, ExecutionEvent]]] = []
        self._finalized = False
        self._terminal: ExecutionEvent | None = None

    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def events(self) -> list[ExecutionEvent]:
        return list(self._events)

    async def append(self, event: ExecutionEvent) -> int:
        if self._finalized:
            raise EventSequenceError(f"sink for run {self.run_id!r} is already finalized")
        if is_terminal(event):
            raise EventSequenceError("terminal events must go through finalize()")
        return await self._append(event)

    async def finalize(self, event: TerminalEvent) -> int:
        if self._finalized:
            raise EventSequenceError(f"sink for run {self.run_id!r} is already finalized")
        if not is_terminal(event):
            raise EventSequenceError("finalize() accepts terminal events only")
        cursor = await self._append(event)
        self._finalized = True
        self._terminal = event
        return cursor

    async def _append(self, event: ExecutionEvent) -> int:
        sequence = len(self._events)
        if event.run_id != self.run_id:
            raise EventSequenceError(
                f"event run_id {event.run_id!r} does not match sink run {self.run_id!r}"
            )
        if event.sequence is not None and event.sequence != sequence:
            raise EventSequenceError(
                f"event arrives with sequence {event.sequence}, expected {sequence}"
            )
        event.sequence = sequence
        cursor = await self._persist(event) if self._persist else sequence
        self._events.append(event)
        self._cursors.append(cursor)
        for queue in self._subscribers:
            queue.put_nowait((cursor, event))
        return cursor

    async def subscribe(
        self, last_cursor: int | None = None
    ) -> AsyncIterator[tuple[int, ExecutionEvent]]:
        """Yield (cursor, event) pairs after `last_cursor`, then live ones,
        ending with the run's terminal event (if any)."""
        queue: asyncio.Queue[tuple[int, ExecutionEvent]] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            # Replay first — the queue was registered before the snapshot, so
            # anything appended meanwhile is deduped by cursor comparison.
            last_yielded = last_cursor
            replay = list(zip(self._cursors, self._events, strict=True))
            terminal_seen = False
            for cursor, event in replay:
                if last_cursor is not None and cursor <= last_cursor:
                    continue
                yield cursor, event
                last_yielded = cursor
                if is_terminal(event):
                    terminal_seen = True
            if terminal_seen or (self._finalized and self._terminal is None):
                return
            while True:
                cursor, event = await queue.get()
                if last_yielded is not None and cursor <= last_yielded:
                    continue
                last_yielded = cursor
                yield cursor, event
                if is_terminal(event):
                    return
        finally:
            self._subscribers.remove(queue)


class InProcessEventBus:
    """Per-run sink registry. `get_or_create` is idempotent so a stream
    endpoint can subscribe before the run task creates/looks up the sink."""

    def __init__(self, persist: PersistFn | None = None) -> None:
        self._persist = persist
        self._sinks: dict[str, InProcessEventSink] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, run_id: str) -> InProcessEventSink:
        async with self._lock:
            if run_id not in self._sinks:
                self._sinks[run_id] = InProcessEventSink(run_id, persist=self._persist)
            return self._sinks[run_id]

    def get(self, run_id: str) -> InProcessEventSink | None:
        return self._sinks.get(run_id)

    def drop(self, run_id: str) -> None:
        self._sinks.pop(run_id, None)

    def active_runs(self) -> list[str]:
        return [run_id for run_id, sink in self._sinks.items() if not sink.finalized]


class ReplayOnlyEventStream:
    """Read side backed purely by persisted events (finished or foreign
    runs). Implements the EventStream port's replay path."""

    def __init__(self, list_events: Any) -> None:
        self._list_events = list_events

    async def replay(self, run_id: str, after: int | None = None) -> AsyncIterator[ExecutionEvent]:
        async for event in self._list_events(run_id, after):
            yield event


__all__ = [
    "InProcessEventBus",
    "InProcessEventSink",
    "PersistFn",
    "ReplayOnlyEventStream",
]
