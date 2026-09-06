"""Worker — claims queued runs and executes them through AgentRuntime.

ADR 0008: the queue is the only execution path; this loop is what makes
runs survive the API process. One `step()` = claim → execute → ack; the
sweeper reaps lost leases (requeue when nothing was emitted, exactly one
terminal failure otherwise — never a blind re-execution, which the per-run
gapless sequence cannot survive).

Collaboration with the runtime: `runtime.run()` itself upserts the RUNNING
row (its Phase 1 `create_run` write becomes the claim), emits every event
through the sink (persist = append_event + pg_notify) and finalizes exactly
once — this file adds only the lease heartbeat (renew + cross-process
cancel poll) around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from jarvis.domain.agent import AgentVersion
from jarvis.domain.events import (
    ExecutionEvent,
    RunCancelled,
    RunCompleted,
    RunFailed,
    is_terminal,
)
from jarvis.domain.execution import ExecutionContext, ExecutionStatus, RunResult
from jarvis.domain.message import Usage
from jarvis.events.bus import InProcessEventSink, PersistFn
from jarvis.ports.queue import RunQueue, RunQueueMessage
from jarvis.runtime.agent_runtime import AgentRuntime

logger = logging.getLogger("jarvis.worker")

LEASE_SECONDS = 15.0
RENEW_INTERVAL_SECONDS = 2.0
SWEEP_INTERVAL_SECONDS = 30.0
CLAIM_POLL_SECONDS = 0.5

_LOST_LEASE_ERROR = "worker lost (lease expired) — run did not finish within its claim"


class VersionLoader(Protocol):
    """Structural view of the agent repo: resolve a version snapshot by id."""

    async def get_version_by_id(self, version_id: str) -> AgentVersion | None: ...


class WorkerExecutions(Protocol):
    """Structural view of the execution repo the worker needs (the port
    stays untouched — the worker sees only what it uses)."""

    async def mark_running(self, run_id: str, started_at: datetime) -> None: ...
    async def count_events(self, run_id: str) -> int: ...
    async def next_event_sequence(self, run_id: str) -> int: ...
    async def latest_event(self, run_id: str) -> tuple[int, ExecutionEvent] | None: ...
    async def append_event(self, event: ExecutionEvent) -> int: ...
    async def finish_run(self, result: RunResult) -> None: ...
    async def get(self, run_id: str) -> RunResult | None: ...


class WorkerNotifier(Protocol):
    """Write-side wake-ups (PgNotifier): fire after the event row commits."""

    async def notify(self, run_id: str, cursor: int) -> None: ...


def worker_persist(executions: WorkerExecutions, notifier: WorkerNotifier) -> PersistFn:
    """The worker's persist callback: append_event (the durable commit), then
    pg_notify the cursor — the DB row is the truth, the notify only wakes."""

    async def persist(event: ExecutionEvent) -> int:
        cursor = await executions.append_event(event)
        await notifier.notify(event.run_id, cursor)
        return cursor

    return persist


class Worker:
    def __init__(
        self,
        *,
        queue: RunQueue,
        versions: VersionLoader,
        executions: WorkerExecutions,
        runtime: AgentRuntime,
        persist: PersistFn,
        worker_id: str | None = None,
        concurrency: int = 4,
        renew_interval: float = RENEW_INTERVAL_SECONDS,
    ) -> None:
        self._queue = queue
        self._versions = versions
        self._executions = executions
        self._runtime = runtime
        self._persist = persist
        self._worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:12]}"
        self._concurrency = concurrency
        self._renew_interval = renew_interval
        self._live: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def live_runs(self) -> list[str]:
        return list(self._live)

    # --- claim/execute loop ---------------------------------------------------

    async def run_forever(self) -> None:
        sweeper = asyncio.create_task(self._sweep_loop())
        try:
            while not self._stopping.is_set():
                if len(self._live) >= self._concurrency:
                    await asyncio.sleep(CLAIM_POLL_SECONDS)
                    continue
                claimed = await self.step()
                if not claimed:
                    await asyncio.sleep(CLAIM_POLL_SECONDS)
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    async def aclose(self) -> None:
        """Stop claiming; cancel in-flight runs (their messages fall back to
        the sweeper's expired-lease policy)."""
        self._stopping.set()
        for task in list(self._live.values()):
            task.cancel()
        if self._live:
            await asyncio.gather(*self._live.values(), return_exceptions=True)
        self._live.clear()

    async def step(self) -> bool:
        """Claim and execute ONE message. False when the queue is empty.
        Never raises — a failed execution leaves the message claimed for the
        sweeper, which is exactly the crash path."""
        message = await self._queue.claim(self._worker_id, timedelta(seconds=LEASE_SECONDS))
        if message is None:
            return False
        task = asyncio.create_task(self._execute_claimed(message))
        self._live[message.run_id] = task

        def _forget(_t: asyncio.Task[None], run_id: str = message.run_id) -> None:
            self._live.pop(run_id, None)

        task.add_done_callback(_forget)
        return True

    async def _execute_claimed(self, message: RunQueueMessage) -> None:
        try:
            sink = InProcessEventSink(message.run_id, persist=self._persist)
            # A cancel that arrived while the run was queued: fail fast,
            # emitting the terminal event without touching the runtime.
            reason = await self._queue.pending_cancel(message.run_id)
            if reason is not None:
                cursor = await sink.finalize(_cancelled_event(message.run_id, reason))
                await self._executions.finish_run(
                    self._result(message, "cancelled", cursor, finished_at=datetime.now(UTC))
                )
                await self._queue.ack(message.run_id)
                return

            version = await self._versions.get_version_by_id(message.agent_version_id)
            if version is None:
                cursor = await sink.finalize(
                    _failed_event(
                        message.run_id,
                        f"agent version {message.agent_version_id!r} not found",
                        "model",
                    )
                )
                await self._executions.finish_run(
                    self._result(
                        message,
                        "failed",
                        cursor,
                        error="agent version not found",
                        error_kind="model",
                        finished_at=datetime.now(UTC),
                    )
                )
                await self._queue.ack(message.run_id)
                return

            # S10: a resumed segment rides the same claim path with the
            # human's answer merged into the payload.
            if message.resume is not None:
                await self._execute_resume(message, version)
                return

            started_at = datetime.now(UTC)
            await self._executions.mark_running(message.run_id, started_at)
            ctx = self._context(message)
            heartbeat = asyncio.create_task(self._heartbeat(message.run_id, ctx))
            try:
                result = await self._runtime.run(version, message.input, ctx, sink=sink)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            await self._queue.ack(message.run_id)
            logger.info("run %s finished: %s", message.run_id, result.status)
        except Exception:  # noqa: BLE001 — the worker loop must never die
            logger.exception("claim execution failed for run %s", message.run_id)

    async def _execute_resume(self, message: RunQueueMessage, version: AgentVersion) -> None:
        """A resumed segment (S10, ADR 0010 §4): the payload's `resume` field
        carries the human's answer. The claim's stale-guard requires the row
        to still be awaiting_input — a reaped pause or an already-resumed run
        absorbs the resume harmlessly (ack + skip, no events)."""
        row = await self._executions.get(message.run_id)
        if row is None or row.status != "awaiting_input":
            await self._queue.ack(message.run_id)
            logger.info(
                "stale resume for run %s (status=%s) — acked, skipped",
                message.run_id,
                getattr(row, "status", None),
            )
            return
        # The segment continues the run's gapless sequence: seed the sink at
        # the durable log's next sequence, not at 0.
        offset = await self._executions.next_event_sequence(message.run_id)
        sink = InProcessEventSink(message.run_id, persist=self._persist, sequence_offset=offset)
        started_at = datetime.now(UTC)
        await self._executions.mark_running(message.run_id, started_at)
        ctx = self._context(message)
        heartbeat = asyncio.create_task(self._heartbeat(message.run_id, ctx))
        try:
            assert message.resume is not None  # the claim branch narrowed it
            await self._runtime.resume(version, message.run_id, ctx, sink, message.resume)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self._queue.ack(message.run_id)
        logger.info("run %s resumed segment finished", message.run_id)

    def _context(self, message: RunQueueMessage) -> ExecutionContext:
        return ExecutionContext(
            run_id=message.run_id,
            agent_id=message.agent_id,
            agent_version_id=message.agent_version_id,
            tenant_id=message.tenant_id,
            principal=message.principal,
            session_id=message.session_id,
            user_id=message.user_id,
            trace_id=message.trace_id,
            metadata=dict(message.metadata),
            variables=dict(message.variables),
            deadline=message.deadline,
        )

    async def _heartbeat(self, run_id: str, ctx: ExecutionContext) -> None:
        """Keep the lease alive and poll for cross-process cancels. A failed
        renewal means this worker may no longer own the run — stop it rather
        than risk double-execution (ADR 0008 §3)."""
        while True:
            await asyncio.sleep(self._renew_interval)
            renewed = await self._queue.renew(
                run_id, self._worker_id, timedelta(seconds=LEASE_SECONDS)
            )
            if not renewed:
                logger.warning("lease lost for run %s — cancelling own execution", run_id)
                ctx.cancel.trigger(_LOST_LEASE_ERROR)
                return
            reason = await self._queue.pending_cancel(run_id)
            if reason is not None:
                ctx.cancel.trigger(reason)
                return

    # --- lease reaping (the sweeper) -------------------------------------------

    async def sweep(self) -> list[str]:
        """Expired leases: requeue runs that emitted nothing (safe to re-run
        from scratch); otherwise record exactly one terminal failure — or
        finish from the terminal event a worker already wrote before dying."""
        acted: list[str] = []
        for run_id in await self._queue.sweep(datetime.now(UTC)):
            try:
                if await self._executions.count_events(run_id) == 0:
                    await self._queue.requeue(run_id)
                    acted.append(run_id)
                    continue
                latest = await self._executions.latest_event(run_id)
                assert latest is not None  # count > 0
                cursor, event = latest
                if is_terminal(event):
                    result = await self._finish_from_terminal(run_id, event, cursor)
                else:
                    cursor = await self._append_lost_lease_failure(run_id)
                    result = await self._finish_lost_lease(run_id, cursor)
                await self._executions.finish_run(result)
                await self._queue.ack(run_id)
                acted.append(run_id)
                logger.warning("reaped expired lease for run %s", run_id)
            except Exception:  # noqa: BLE001 — one bad run must not stop the sweep
                logger.exception("sweep failed for run %s", run_id)
        return acted

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self.sweep()

    async def _append_lost_lease_failure(self, run_id: str) -> int:
        event = _failed_event(run_id, _LOST_LEASE_ERROR, "timeout")
        event.sequence = await self._executions.next_event_sequence(run_id)
        return await self._executions.append_event(event)

    async def _finish_lost_lease(self, run_id: str, cursor: int) -> RunResult:
        return await self._finish_from_row(
            run_id,
            update={
                "status": "failed",
                "error": _LOST_LEASE_ERROR,
                "error_kind": "timeout",
                "finished_at": datetime.now(UTC),
                "event_cursor": cursor,
            },
        )

    async def _finish_from_terminal(
        self, run_id: str, event: ExecutionEvent, cursor: int
    ) -> RunResult:
        """Crash between sink.finalize() and finish_run(): the terminal event
        is durable — finish the row from it instead of inventing a state."""
        update: dict[str, object]
        if isinstance(event, RunCompleted):
            update = {
                "status": "succeeded",
                "final_message": event.final_message,
                "iterations": event.iterations,
                "finished_at": datetime.now(UTC),
                "event_cursor": cursor,
            }
        elif isinstance(event, RunCancelled):
            update = {
                "status": "cancelled",
                "finished_at": datetime.now(UTC),
                "event_cursor": cursor,
            }
        else:
            assert isinstance(event, RunFailed)
            update = {
                "status": "failed",
                "error": event.error,
                "error_kind": event.error_kind,
                "finished_at": datetime.now(UTC),
                "event_cursor": cursor,
            }
        return await self._finish_from_row(run_id, update)

    async def _finish_from_row(self, run_id: str, update: dict[str, object]) -> RunResult:
        row = await self._executions.get(run_id)
        assert row is not None  # the execution row outlives its queue message
        return row.model_copy(update=update)

    def _result(
        self,
        message: RunQueueMessage,
        status: ExecutionStatus,
        cursor: int,
        *,
        error: str | None = None,
        error_kind: str | None = None,
        finished_at: datetime | None = None,
    ) -> RunResult:
        return RunResult(
            run_id=message.run_id,
            agent_id=message.agent_id,
            status=status,
            input=message.input,
            agent_version_id=message.agent_version_id,
            tenant_id=message.tenant_id,
            session_id=message.session_id,
            trace_id=message.trace_id,
            error=error,
            error_kind=error_kind,
            started_at=datetime.now(UTC),
            finished_at=finished_at or datetime.now(UTC),
            event_cursor=cursor,
        )


def _cancelled_event(run_id: str, reason: str) -> RunCancelled:
    return RunCancelled(
        event_id=str(uuid4()),
        run_id=run_id,
        created_at=datetime.now(UTC),
        reason=reason,
        total_usage=Usage(),
    )


def _failed_event(run_id: str, error: str, error_kind: str) -> RunFailed:
    return RunFailed(
        event_id=str(uuid4()),
        run_id=run_id,
        created_at=datetime.now(UTC),
        error=error,
        error_kind=error_kind,  # type: ignore[arg-type]
        total_usage=Usage(),
    )


__all__ = ["LEASE_SECONDS", "Worker", "worker_persist"]
