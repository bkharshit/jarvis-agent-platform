"""LISTEN/NOTIFY event fan-out — the queue-backed live bus (ADR 0008 §4).

The worker's sink persists events (unchanged `append_event` commit) and then
`pg_notify`s the cursor; subscribers *tail the DB by global cursor* and use
NOTIFY only as a wake-up. The database is the sole source of truth, so a
missed NOTIFY costs latency (the fallback poll), never correctness —
delivery stays exactly-once because every yielded cursor is yielded once.

`PgEventStream` holds one dedicated asyncpg LISTEN connection (a pooled
SQLAlchemy connection cannot hold a LISTEN). There is no terminate callback
in asyncpg, so health is checked at each poll: a dead connection is
reconnected on the spot and the fallback poll keeps events flowing meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.domain.events import ExecutionEvent, is_terminal
from jarvis.domain.execution import TERMINAL_STATUSES

logger = logging.getLogger("jarvis.events")

EVENT_CHANNEL = "jarvis_events"
# NOTIFY is a wake-up only — a subscriber never depends on receiving one.
WAKE_TIMEOUT_SECONDS = 1.0
_REPLAY_CURSOR_FN = Callable[[str, int | None], AsyncIterator[tuple[int, ExecutionEvent]]]
_RUN_STATUS_FN = Callable[[str], Any]  # async run_id -> RunResult | None

_NotifyPayload = tuple[str, int]  # (run_id, cursor)


def _asyncpg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


class PgNotifier:
    """Write-side wake-ups. Best-effort by design: callers already committed
    the event row, so a failed notify can only delay a subscriber."""

    def __init__(self, database_url: str) -> None:
        self._dsn = _asyncpg_dsn(database_url)
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    async def notify(self, run_id: str, cursor: int) -> None:
        try:
            await self._send(run_id, cursor)
        except Exception:  # noqa: BLE001 — a missed notify is a slower subscriber
            logger.warning(
                "pg_notify failed for run %s cursor %s — subscribers fall back to polling",
                run_id,
                cursor,
            )

    async def _send(self, run_id: str, cursor: int) -> None:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        payload = json.dumps({"run_id": run_id, "cursor": cursor})
        await self._pool.execute("SELECT pg_notify($1, $2)", EVENT_CHANNEL, payload)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class PgEventStream:
    """Read side over Postgres: replay from `execution_events`, live via
    LISTEN/NOTIFY wake-ups. One instance serves many subscribers and
    implements the `EventStream` port."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],  # noqa: ARG002 — replay fn owns SQL
        database_url: str,
    ) -> None:
        self._dsn = _asyncpg_dsn(database_url)
        self._replay_with_cursor: _REPLAY_CURSOR_FN | None = None
        self._run_status: _RUN_STATUS_FN | None = None
        self._connection: asyncpg.Connection | None = None
        self._subscribers: list[asyncio.Queue[_NotifyPayload]] = []
        self._lock = asyncio.Lock()

    def replay_cursor_fn(self, fn: _REPLAY_CURSOR_FN) -> None:
        """Inject the repo's cursor-space replay — the repo stays the one SQL
        owner for `execution_events`."""
        self._replay_with_cursor = fn

    def run_status_fn(self, fn: _RUN_STATUS_FN) -> None:
        """Inject the repo's run lookup — lets a subscriber whose replay came
        up empty tell a finished run from a run with nothing new yet."""
        self._run_status = fn

    # --- listener lifecycle ---------------------------------------------------

    async def _ensure_listening(self) -> None:
        async with self._lock:
            if self._connection is not None and not self._connection.is_closed():
                return
            connection = await asyncpg.connect(self._dsn)
            # asyncpg ≥ 0.30: add_listener is awaited (it registers on the
            # socket lazily); older releases took a sync callback.
            await connection.add_listener(EVENT_CHANNEL, self._on_notify)
            self._connection = connection

    async def _heal_if_needed(self) -> None:
        if self._connection is None or self._connection.is_closed():
            with contextlib.suppress(Exception):
                if self._connection is not None:
                    await self._connection.close()
            self._connection = None
            try:
                await self._ensure_listening()
            except Exception:  # noqa: BLE001 — the fallback poll keeps working
                logger.warning("LISTEN reconnect failed — subscribers keep polling")

    def _on_notify(
        self, _connection: asyncpg.Connection, _pid: int, _channel: str, payload: str
    ) -> None:
        try:
            data = json.loads(payload)
            item: _NotifyPayload = (data["run_id"], int(data["cursor"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("dropping malformed notify payload: %r", payload)
            return
        for queue in list(self._subscribers):
            queue.put_nowait(item)

    # --- EventStream port -------------------------------------------------------

    async def subscribe(
        self, run_id: str, last_cursor: int | None = None
    ) -> AsyncIterator[tuple[int, ExecutionEvent]]:
        if self._replay_with_cursor is None:
            raise RuntimeError("replay_cursor_fn not wired")
        await self._ensure_listening()
        queue: asyncio.Queue[_NotifyPayload] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            last_yielded = last_cursor
            while True:
                # Materialize the batch first: a terminal stop must not leave
                # an async replay generator (and its session) dangling.
                batch = [pair async for pair in self._replay_with_cursor(run_id, last_yielded)]
                terminal_seen = False
                for cursor, event in batch:
                    last_yielded = cursor
                    yield cursor, event
                    if is_terminal(event):
                        terminal_seen = True
                if terminal_seen:
                    return
                if not batch and self._run_status is not None:
                    # Empty replay: either the run is still live (nothing new
                    # since `last_cursor`) or the client already consumed the
                    # terminal event before reconnecting — in which case the
                    # stream ends now, as a pure replay would have. An unknown
                    # row keeps waiting: every route path creates the row
                    # before subscribing (404 happens first).
                    run = await self._run_status(run_id)
                    if run is not None and run.status in TERMINAL_STATUSES:
                        return
                await self._wait_for_wake(queue, run_id)
                await self._heal_if_needed()
        finally:
            self._subscribers.remove(queue)

    async def _wait_for_wake(self, queue: asyncio.Queue[_NotifyPayload], run_id: str) -> None:
        """Block until THIS run gets a notify — or the fallback poll timeout
        expires, which is equivalent (the loop re-queries the DB either way)."""
        while True:
            try:
                wake_run, _cursor = await asyncio.wait_for(
                    queue.get(), timeout=WAKE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                return
            if wake_run == run_id:
                return

    async def replay(self, run_id: str, after: int | None = None) -> AsyncIterator[ExecutionEvent]:
        if self._replay_with_cursor is None:
            raise RuntimeError("replay_cursor_fn not wired")
        async for _cursor, event in self._replay_with_cursor(run_id, after):
            yield event

    async def aclose(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None


__all__ = ["EVENT_CHANNEL", "PgEventStream", "PgNotifier"]
