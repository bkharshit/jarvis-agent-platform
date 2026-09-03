"""Event sink/stream protocols — Protocols ONLY.

ADR 0003: `append` assigns the per-run gapless sequence and returns a cursor;
`finalize` accepts terminal events only, is once-only, and poisons the sink.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from jarvis.domain.events import EventSequenceError, ExecutionEvent, TerminalEvent


class EventSink(Protocol):
    """Write side. One sink instance per run (sequence is per-run)."""

    async def append(self, event: ExecutionEvent) -> int:
        """Assign the next gapless per-run sequence, persist/dispatch, and
        return the cursor. Raises EventSequenceError if the event is
        terminal or the sink is already finalized."""
        ...

    async def finalize(self, event: TerminalEvent) -> int:
        """Append the terminal event (terminal only, exactly once) and close
        the run's stream."""
        ...

    @property
    def finalized(self) -> bool: ...


class EventStream(Protocol):
    """Read side: replay + live subscription, cursor-based."""

    def subscribe(
        self, run_id: str, last_cursor: int | None = None
    ) -> AsyncIterator[ExecutionEvent]:
        """Yield replayed events after `last_cursor`, then live events,
        ending with the run's terminal event."""
        ...

    def replay(self, run_id: str, after: int | None = None) -> AsyncIterator[ExecutionEvent]:
        """Yield persisted events only."""
        ...


__all__ = ["EventSequenceError", "EventSink", "EventStream", "TerminalEvent"]