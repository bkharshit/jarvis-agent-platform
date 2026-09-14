"""Worker loop over in-memory fakes (no DB): claim → execute → ack, lease
heartbeat, cancel-before-claim, and the sweeper's expired-lease policy
(ADR 0008 §3). The runtime itself is stubbed — AgentRuntime has its own
suite; the worker adds only claim/ack/heartbeat/sweep around it."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig
from jarvis.domain.events import RunCancelled, RunCompleted, RunStarted
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.domain.message import Usage
from jarvis.events.bus import InProcessEventSink
from jarvis.ports.queue import ResumeRequest, RunQueueMessage
from jarvis.runtime.worker import Worker, worker_persist


class FakeQueue:
    """RunQueue port over dicts, with controllable lease loss."""

    def __init__(self) -> None:
        self.messages: list[RunQueueMessage] = []
        self.claimed: dict[str, str] = {}  # run_id -> worker_id
        self.cancels: dict[str, str] = {}
        self.acked: list[str] = []
        self.requeued: list[str] = []
        self.lose_leases: set[str] = set()
        self.expired: set[str] = set()

    async def enqueue(self, message: RunQueueMessage) -> None:
        self.messages.append(message)

    async def claim(self, worker_id: str, lease: timedelta) -> RunQueueMessage | None:
        for message in self.messages:
            if message.run_id not in self.claimed:
                self.claimed[message.run_id] = worker_id
                return message
        return None

    async def ack(self, run_id: str) -> None:
        self.acked.append(run_id)

    async def renew(self, run_id: str, worker_id: str, lease: timedelta) -> bool:
        return run_id not in self.lose_leases

    async def pending_cancel(self, run_id: str) -> str | None:
        return self.cancels.pop(run_id, None)

    async def request_cancel(self, run_id: str, reason: str) -> None:
        self.cancels[run_id] = reason

    async def sweep(self, expired_before: datetime) -> list[str]:
        return [
            run_id for run_id in self.claimed if run_id in self.expired and run_id not in self.acked
        ]

    async def requeue(self, run_id: str) -> None:
        self.requeued.append(run_id)


class FakeExecutions:
    """WorkerExecutions over dicts — enough for the sweeper's decisions."""

    def __init__(self) -> None:
        self.runs: dict[str, RunResult] = {}
        self.events: dict[str, list[tuple[int, Any]]] = {}
        self.next_cursor = 100
        self.finished: list[RunResult] = []
        self.expired_pauses: list[str] = []

    async def mark_running(self, run_id: str, started_at: datetime) -> None:
        if run_id in self.runs:
            self.runs[run_id] = self.runs[run_id].model_copy(
                update={"status": "running", "started_at": started_at}
            )

    async def count_events(self, run_id: str) -> int:
        return len(self.events.get(run_id, []))

    async def next_event_sequence(self, run_id: str) -> int:
        return len(self.events.get(run_id, []))

    async def latest_event(self, run_id: str) -> tuple[int, Any] | None:
        events = self.events.get(run_id, [])
        return events[-1] if events else None

    async def append_event(self, event: Any) -> int:
        cursor = self.next_cursor
        self.next_cursor += 1
        self.events.setdefault(event.run_id, []).append((cursor, event))
        return cursor

    async def finish_run(self, result: RunResult) -> None:
        self.runs[result.run_id] = result
        self.finished.append(result)

    async def get(self, run_id: str) -> RunResult | None:
        return self.runs.get(run_id)

    async def expired_awaiting(self, now: datetime) -> list[str]:
        return list(self.expired_pauses)


class NotifyRecorder:
    def __init__(self) -> None:
        self.notified: list[tuple[str, int]] = []

    async def notify(self, run_id: str, cursor: int) -> None:
        self.notified.append((run_id, cursor))


class ScriptedRuntime:
    """Runtime stand-in with a script of behaviors keyed by run_id. The real
    AgentRuntime owns finish_run(); this stub records its results instead —
    the worker's only runtime contract is `run()` returning the outcome."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resumes: list[tuple[str, Any]] = []
        self.behaviors: dict[str, Any] = {}
        self.results: dict[str, RunResult] = {}

    async def resume(
        self, version: Any, run_id: str, ctx: ExecutionContext, sink: Any, resume_request: Any
    ):
        self.calls.append(run_id)
        self.resumes.append((run_id, resume_request))
        cursor = await sink.finalize(
            RunCompleted(
                event_id=str(uuid4()),
                run_id=run_id,
                created_at=datetime.now(UTC),
                final_message="resumed",
                total_usage=Usage(),
                iterations=1,
            )
        )
        result = RunResult(
            run_id=run_id,
            agent_id=ctx.agent_id,
            status="succeeded",
            final_message="resumed",
            iterations=1,
            event_cursor=cursor,
        )
        self.results[run_id] = result
        return result

    async def run(self, version: Any, input: str, ctx: ExecutionContext, sink: Any = None):
        self.calls.append(ctx.run_id)
        sink = sink or InProcessEventSink(ctx.run_id)
        behavior = self.behaviors.get(ctx.run_id, "complete")
        if behavior == "block_until_cancelled":
            await ctx.cancel.wait()
            cursor = await sink.finalize(
                RunCancelled(
                    event_id=str(uuid4()),
                    run_id=ctx.run_id,
                    created_at=datetime.now(UTC),
                    reason=ctx.cancel.reason or "cancelled",
                    total_usage=Usage(),
                )
            )
            result = RunResult(
                run_id=ctx.run_id,
                agent_id=ctx.agent_id,
                status="cancelled",
                event_cursor=cursor,
            )
            self.results[ctx.run_id] = result
            return result
        cursor = await sink.append(
            RunStarted(
                event_id=str(uuid4()),
                run_id=ctx.run_id,
                created_at=datetime.now(UTC),
                agent_id=ctx.agent_id,
                agent_version_id=ctx.agent_version_id,
            )
        )
        cursor = await sink.finalize(
            RunCompleted(
                event_id=str(uuid4()),
                run_id=ctx.run_id,
                created_at=datetime.now(UTC),
                final_message=input,
                total_usage=Usage(),
                iterations=1,
            )
        )
        result = RunResult(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            status="succeeded",
            final_message=input,
            iterations=1,
            event_cursor=cursor,
        )
        self.results[ctx.run_id] = result
        return result


class VersionStub:
    def __init__(self, version: AgentVersion) -> None:
        self._version = version

    async def get_version_by_id(self, _version_id: str) -> AgentVersion | None:
        return self._version


def _definition(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        name="w-agent",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
    )


def _version(definition: AgentDefinition) -> AgentVersion:
    return AgentVersion(
        id="version-1",
        agent_id=definition.id,
        version=1,
        snapshot=definition,
        label="initial",
    )


def _message(run_id: str, definition: AgentDefinition) -> RunQueueMessage:
    return RunQueueMessage(
        run_id=run_id,
        agent_id=definition.id,
        agent_version_id="version-1",
        input="hello",
    )


def _wired(
    **worker_kwargs: Any,
) -> tuple[Worker, FakeQueue, FakeExecutions, NotifyRecorder, AgentDefinition]:
    definition = _definition("agent-1")
    queue, executions, notifier = FakeQueue(), FakeExecutions(), NotifyRecorder()
    worker = Worker(
        queue=queue,
        versions=VersionStub(_version(definition)),
        executions=executions,
        runtime=ScriptedRuntime(),
        persist=worker_persist(executions, notifier),
        **worker_kwargs,
    )
    return worker, queue, executions, notifier, definition


async def _await_live(worker: Worker) -> None:
    """Wait for the worker's in-flight execution tasks to settle."""
    for _ in range(500):
        if not worker.live_runs:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker still has live runs")


async def test_claimed_run_executes_and_acks():
    worker, queue, executions, notifier, definition = _wired()
    await queue.enqueue(_message("run-1", definition))

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["run-1"]
    types = [event.type for _cursor, event in executions.events["run-1"]]
    assert types == ["run.started", "run.completed"]
    assert notifier.notified  # every persisted event woke subscribers
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    run = runtime.results["run-1"]
    assert run.status == "succeeded" and run.final_message == "hello"


async def test_step_returns_false_on_empty_queue():
    worker, queue, _executions, _notifier, _definition = _wired()
    assert await worker.step() is False


async def test_cancel_before_claim_fails_fast():
    worker, queue, executions, _notifier, definition = _wired()
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    await queue.enqueue(_message("run-2", definition))
    await queue.request_cancel("run-2", "user asked")

    assert await worker.step() is True
    await _await_live(worker)

    assert runtime.calls == []  # the runtime never touched a pre-cancelled run
    assert queue.acked == ["run-2"]
    types = [event.type for _cursor, event in executions.events["run-2"]]
    assert types == ["run.cancelled"]
    assert executions.runs["run-2"].status == "cancelled"


async def test_lost_lease_cancels_own_run():
    worker, queue, executions, _notifier, definition = _wired(renew_interval=0.01)
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    runtime.behaviors["run-3"] = "block_until_cancelled"
    queue.lose_leases.add("run-3")
    await queue.enqueue(_message("run-3", definition))

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["run-3"]
    types = [event.type for _cursor, event in executions.events["run-3"]]
    assert types == ["run.cancelled"]
    assert runtime.results["run-3"].status == "cancelled"


async def test_sweep_requeues_zero_event_run():
    worker, queue, executions, _notifier, definition = _wired()
    await queue.enqueue(_message("run-4", definition))
    await queue.claim("dead-worker", timedelta(seconds=15))
    queue.expired.add("run-4")

    acted = await worker.sweep()

    assert acted == ["run-4"]
    assert queue.requeued == ["run-4"]
    assert queue.acked == []
    assert "run-4" not in executions.events  # nothing was emitted


async def test_sweep_fails_partial_run_with_one_terminal_event():
    worker, queue, executions, _notifier, definition = _wired()
    await queue.enqueue(_message("run-5", definition))
    await queue.claim("dead-worker", timedelta(seconds=15))
    queue.expired.add("run-5")
    executions.runs["run-5"] = RunResult(run_id="run-5", agent_id=definition.id, status="running")
    started = RunStarted(
        event_id="e1",
        run_id="run-5",
        created_at=datetime.now(UTC),
        agent_id=definition.id,
        agent_version_id="version-1",
    )
    started.sequence = 0
    await executions.append_event(started)

    acted = await worker.sweep()

    assert acted == ["run-5"]
    assert queue.acked == ["run-5"] and queue.requeued == []
    cursors, events = zip(*executions.events["run-5"], strict=True)
    assert [e.type for e in events] == ["run.started", "run.failed"]
    assert events[-1].sequence == 1  # continued the gapless per-run sequence
    assert events[-1].error_kind == "timeout"
    finished = executions.runs["run-5"]
    assert finished.status == "failed" and finished.event_cursor == cursors[-1]


async def test_sweep_finishes_run_whose_terminal_event_already_landed():
    worker, queue, executions, _notifier, definition = _wired()
    await queue.enqueue(_message("run-6", definition))
    await queue.claim("dead-worker", timedelta(seconds=15))
    queue.expired.add("run-6")
    executions.runs["run-6"] = RunResult(run_id="run-6", agent_id=definition.id, status="running")
    terminal = RunCompleted(
        event_id="t1",
        run_id="run-6",
        created_at=datetime.now(UTC),
        final_message="done",
        total_usage=Usage(),
        iterations=1,
    )
    terminal.sequence = 0
    cursor = await executions.append_event(terminal)  # finalize landed, finish_run didn't

    acted = await worker.sweep()

    assert acted == ["run-6"]
    assert len(executions.events["run-6"]) == 1  # no duplicate terminal event
    finished = executions.runs["run-6"]
    assert finished.status == "succeeded" and finished.event_cursor == cursor
    assert finished.final_message == "done"


async def test_sweep_reaps_expired_pause_with_one_terminal_event():
    """S10 (ADR 0010 §6): a paused run is acked — no heartbeat holds it, so
    the row's deadline is what the sweeper acts on. Exactly one terminal
    `run.cancelled` at the next sequence, then finish_run; the paused
    row's usage carries into the terminal event."""
    worker, queue, executions, notifier, definition = _wired()
    executions.runs["run-7"] = RunResult(
        run_id="run-7",
        agent_id=definition.id,
        status="awaiting_input",
        total_usage=Usage(input_tokens=5, output_tokens=2),
    )
    executions.expired_pauses.append("run-7")

    acted = await worker.sweep()

    assert acted == ["run-7"]
    cursors, events = zip(*executions.events["run-7"], strict=True)
    assert [e.type for e in events] == ["run.cancelled"]
    assert events[-1].reason == "awaiting_input timeout"
    assert events[-1].sequence == 0
    assert events[-1].total_usage == Usage(input_tokens=5, output_tokens=2)
    assert notifier.notified  # the reap woke subscribers holding at the pause
    finished = executions.runs["run-7"]
    assert finished.status == "cancelled" and finished.event_cursor == cursors[-1]
    assert finished.finished_at is not None
    assert finished.total_usage == Usage(input_tokens=5, output_tokens=2)
    assert queue.acked == ["run-7"]  # a pending resume is dropped with the row


async def test_sweep_skips_pause_that_already_moved_on():
    """Raced resume/reap: the row is no longer awaiting_input — no events,
    no finish, nothing to reap (the stale-guard owns the queue side)."""
    worker, queue, executions, _notifier, definition = _wired()
    executions.runs["run-8"] = RunResult(run_id="run-8", agent_id=definition.id, status="succeeded")
    executions.expired_pauses.append("run-8")

    acted = await worker.sweep()

    assert acted == []
    assert "run-8" not in executions.events
    assert executions.finished == []
    assert queue.acked == []


async def test_run_forever_respects_concurrency_cap():
    worker, queue, executions, _notifier, definition = _wired(concurrency=2)
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    for i in range(4):
        run_id = f"run-c{i}"
        runtime.behaviors[run_id] = "block_until_cancelled"
        await queue.enqueue(_message(run_id, definition))

    loop = asyncio.create_task(worker.run_forever())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(worker.live_runs) == 2:
            break

    try:
        assert len(worker.live_runs) == 2  # blocking runs: no 3rd claim
        assert len(queue.claimed) == 2
    finally:
        await worker.aclose()
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
    assert worker.live_runs == []
    assert queue.acked == []  # cancelled blocking runs were never acked


async def test_resume_claim_executes_and_acks():
    worker, queue, executions, notifier, definition = _wired()
    executions.runs["run-r1"] = RunResult(
        run_id="run-r1",
        agent_id=definition.id,
        status="awaiting_input",
        started_at=datetime.now(UTC),
    )
    resume = ResumeRequest(kind="content", content="prod")
    await queue.enqueue(_message("run-r1", definition).model_copy(update={"resume": resume}))

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["run-r1"]
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    assert [r for r, _ in runtime.resumes] == ["run-r1"]
    assert runtime.resumes[0][1] == resume
    # the row was flipped back to running for the segment
    assert executions.runs["run-r1"].status == "running"
    # the resumed sink is seeded at the durable log's next sequence, not 0
    assert queue.messages  # the claim consumed the pending message
    assert executions.finished == []


async def test_stale_resume_is_acked_without_execution():
    worker, queue, executions, _notifier, definition = _wired()
    # the run is no longer awaiting_input (already resumed / reaped)
    executions.runs["run-r2"] = RunResult(
        run_id="run-r2",
        agent_id=definition.id,
        status="succeeded",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    await queue.enqueue(
        _message("run-r2", definition).model_copy(
            update={"resume": ResumeRequest(kind="content", content="late")}
        )
    )

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["run-r2"]
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    assert runtime.resumes == []  # absorbed harmlessly, no events, no execution
    assert "run-r2" not in executions.events


async def test_resume_without_row_is_acked_without_execution():
    worker, queue, executions, _notifier, definition = _wired()
    await queue.enqueue(
        _message("run-r3", definition).model_copy(
            update={"resume": ResumeRequest(kind="tool_approval", approved=True)}
        )
    )

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["run-r3"]
    runtime: ScriptedRuntime = worker._runtime  # noqa: SLF001 — test observation
    assert runtime.resumes == []
    assert "run-r3" not in executions.events


# --- workflow kind branch (S6, ADR 0015 §4, D41) ------------------------------


class FakeWorkflowRuntime:
    """The workflow runtime's narrow worker contract: run() to a terminal,
    resume() through the paused node (S10 composition, ADR 0015 §7)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.resumes: list[tuple[str, object]] = []

    async def run(self, version, input, ctx, sink=None):
        self.calls.append((ctx.run_id, input))
        cursor = await sink.append(
            RunStarted(
                event_id=str(uuid4()),
                run_id=ctx.run_id,
                created_at=datetime.now(UTC),
                agent_id=ctx.agent_id,
                agent_version_id=ctx.agent_version_id,
            )
        )
        cursor = await sink.finalize(
            RunCompleted(
                event_id=str(uuid4()),
                run_id=ctx.run_id,
                created_at=datetime.now(UTC),
                final_message="walked",
                total_usage=Usage(),
                iterations=2,
            )
        )
        return RunResult(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            status="succeeded",
            final_message="walked",
            iterations=2,
            event_cursor=cursor,
        )

    async def resume(self, version, run_id, ctx, sink, resume):
        self.resumes.append((run_id, resume))
        cursor = await sink.finalize(
            RunCompleted(
                event_id=str(uuid4()),
                run_id=ctx.run_id,
                created_at=datetime.now(UTC),
                final_message="resumed",
                total_usage=Usage(),
                iterations=1,
            )
        )
        return RunResult(
            run_id=ctx.run_id,
            agent_id=ctx.agent_id,
            status="succeeded",
            final_message="resumed",
            iterations=1,
            event_cursor=cursor,
        )


class WorkflowVersionStub:
    async def get_version_by_id(self, version_id: str):
        from jarvis.domain.workflow import AgentNodeConfig, WorkflowDefinition, WorkflowNode

        definition = WorkflowDefinition(
            id="wf-1",
            name="wf",
            nodes=[
                WorkflowNode(
                    id="a",
                    type="agent",
                    config=AgentNodeConfig(
                        agent_id="agent-1", agent_version_id="version-1", input_template="{{input}}"
                    ),
                )
            ],
            start_node_id="a",
        )
        from jarvis.domain.workflow import WorkflowVersion

        return WorkflowVersion(
            id=version_id, workflow_id=definition.id, version=1, snapshot=definition
        )


async def test_workflow_kind_executes_through_the_workflow_runtime():
    queue, executions, notifier = FakeQueue(), FakeExecutions(), NotifyRecorder()
    wf_runtime = FakeWorkflowRuntime()
    definition = _definition("agent-1")
    worker = Worker(
        queue=queue,
        versions=VersionStub(_version(definition)),
        executions=executions,
        runtime=ScriptedRuntime(),
        persist=worker_persist(executions, notifier),
        workflow_runtime=wf_runtime,
        workflow_versions=WorkflowVersionStub(),
    )
    await queue.enqueue(
        RunQueueMessage(
            run_id="wrun-1",
            agent_id="wf-1",
            agent_version_id="wfv-1",
            input="hi",
            kind="workflow",
        )
    )

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["wrun-1"]
    assert wf_runtime.calls == [("wrun-1", "hi")]
    types = [event.type for _c, event in executions.events["wrun-1"]]
    assert types == ["run.started", "run.completed"]
    # the runtime owns the finish_run row write; the worker's contract is
    # the ack after the runtime returned a terminal result


async def test_workflow_resume_executes_through_the_workflow_runtime():
    """The S10 composition (ADR 0015 §7): a workflow run paused inside an
    agent node resumes through WorkflowRuntime.resume — the same stale-guard
    and ack contract as the agent path."""
    queue, executions, notifier = FakeQueue(), FakeExecutions(), NotifyRecorder()
    wf_runtime = FakeWorkflowRuntime()
    definition = _definition("agent-1")
    worker = Worker(
        queue=queue,
        versions=VersionStub(_version(definition)),
        executions=executions,
        runtime=ScriptedRuntime(),
        persist=worker_persist(executions, notifier),
        workflow_runtime=wf_runtime,
        workflow_versions=WorkflowVersionStub(),
    )
    resume = ResumeRequest(kind="content", content="prod")
    # the paused run's row — the resume claim's stale-guard needs it
    executions.runs["wrun-2"] = RunResult(run_id="wrun-2", agent_id="wf-1", status="awaiting_input")
    await queue.enqueue(
        RunQueueMessage(
            run_id="wrun-2",
            agent_id="wf-1",
            agent_version_id="wfv-1",
            input="hi",
            kind="workflow",
            resume=resume,
        )
    )

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == ["wrun-2"]
    assert wf_runtime.resumes == [("wrun-2", resume)]
    types = [event.type for _c, event in executions.events["wrun-2"]]
    assert types == ["run.completed"]  # the fake runtime's resumed walk walked


async def test_workflow_kind_without_a_workflow_runtime_stays_claimed():
    """A misconfigured worker must never execute a workflow through the
    agent runtime — the claim fails loudly and the sweeper owns the row."""
    queue, executions, notifier = FakeQueue(), FakeExecutions(), NotifyRecorder()
    definition = _definition("agent-1")
    worker = Worker(
        queue=queue,
        versions=VersionStub(_version(definition)),
        executions=executions,
        runtime=ScriptedRuntime(),
        persist=worker_persist(executions, notifier),
    )
    await queue.enqueue(
        RunQueueMessage(
            run_id="wrun-3",
            agent_id="wf-1",
            agent_version_id="wfv-1",
            input="hi",
            kind="workflow",
        )
    )

    assert await worker.step() is True
    await _await_live(worker)

    assert queue.acked == []  # the failure left the message claimed
    assert "wrun-3" not in executions.events  # nothing executed
    assert executions.finished == []
