"""S14 (ADR 0018 §3, D53): the additive `parent_run_id` linkage — envelope
additivity (old payloads replay as None), the sink's single stamping choke
point, queue-message parse, and the worker's threading into
ExecutionContext + sink."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from jarvis.domain.events import RunCompleted, RunStarted
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.domain.message import Usage
from jarvis.events.bus import InProcessEventSink
from jarvis.ports.queue import RunQueueMessage
from tests.unit.test_worker import _await_live, _wired


def _message_payload() -> dict[str, object]:
    """A pre-S14 RunQueueMessage payload (extra="forbid" — must still parse)."""
    return {
        "run_id": "run-1",
        "agent_id": "agent-1",
        "agent_version_id": "version-1",
        "input": "hello",
        "trace_id": "t-1",
    }


def _event_payload() -> dict[str, object]:
    """A pre-S14 persisted event envelope (no parent_run_id)."""
    return {
        "event_id": str(uuid4()),
        "run_id": "run-1",
        "sequence": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "type": "run.started",
        "agent_id": "agent-1",
        "agent_version_id": "version-1",
        "input": "hello",
    }


class TestEnvelopeAdditivity:
    def test_old_queue_message_parses_without_parent_run_id(self):
        from jarvis.ports.queue import RunQueueMessage

        message = RunQueueMessage.model_validate(_message_payload())
        assert message.parent_run_id is None

    def test_old_event_envelope_parses_without_parent_run_id(self):
        event = RunStarted.model_validate(_event_payload())
        assert event.parent_run_id is None
        assert event.type == "run.started"

    def test_new_field_roundtrips(self):
        from jarvis.ports.queue import RunQueueMessage

        message = RunQueueMessage(
            run_id="run-1",
            agent_id="agent-1",
            agent_version_id="version-1",
            input="hello",
            parent_run_id="parent-1",
        )
        restored = RunQueueMessage.model_validate(message.model_dump(mode="json"))
        assert restored.parent_run_id == "parent-1"

    def test_terminal_event_carries_the_field(self):
        completed = RunCompleted(
            event_id=str(uuid4()),
            run_id="run-1",
            final_message="done",
            total_usage=Usage(),
            iterations=1,
        )
        assert completed.parent_run_id is None


class TestSinkStamping:
    async def test_parent_sink_stamps_every_event(self):
        sink = InProcessEventSink("run-1", parent_run_id="parent-1")
        started = RunStarted(
            event_id=str(uuid4()),
            run_id="run-1",
            agent_id="agent-1",
            agent_version_id="version-1",
        )
        await sink.append(started)
        cursor = await sink.finalize(
            RunCompleted(
                event_id=str(uuid4()),
                run_id="run-1",
                final_message="done",
                total_usage=Usage(),
                iterations=1,
            )
        )
        assert cursor == 1
        assert [event.parent_run_id for event in sink.events] == ["parent-1", "parent-1"]

    async def test_root_sink_leaves_parent_run_id_none(self):
        sink = InProcessEventSink("run-1")
        await sink.append(
            RunStarted(
                event_id=str(uuid4()),
                run_id="run-1",
                agent_id="agent-1",
                agent_version_id="version-1",
            )
        )
        assert sink.events[0].parent_run_id is None


class TestDomainFields:
    def test_execution_context_defaults_to_root(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v")
        assert ctx.parent_run_id is None

    def test_run_result_defaults_to_root(self):
        result = RunResult(run_id="r", agent_id="a", status="succeeded")
        assert result.parent_run_id is None

    def test_execution_context_carries_parent(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v", parent_run_id="p")
        assert ctx.parent_run_id == "p"


class TestWorkerThreading:
    async def test_context_carries_message_parent(self):
        from jarvis.ports.queue import RunQueueMessage

        worker, _queue, _executions, _notifier, definition = _wired()
        message = RunQueueMessage(
            run_id="child-1",
            agent_id=definition.id,
            agent_version_id="version-1",
            input="hello",
            parent_run_id="parent-1",
        )
        ctx = worker._context(message)  # noqa: SLF001 — test observation
        assert ctx.parent_run_id == "parent-1"

    async def test_child_events_carry_parent_run_id(self):
        """The worker constructs the sink from the message — a child run's
        events are stamped with the parent linkage end to end."""
        worker, queue, executions, _notifier, definition = _wired()
        await queue.enqueue(
            RunQueueMessage(
                run_id="child-1",
                agent_id=definition.id,
                agent_version_id="version-1",
                input="hello",
                parent_run_id="parent-1",
            )
        )
        assert await worker.step() is True
        await _await_live(worker)
        events = [event for _cursor, event in executions.events["child-1"]]
        assert events
        assert all(event.parent_run_id == "parent-1" for event in events)

    async def test_root_events_have_no_parent(self):
        worker, queue, executions, _notifier, definition = _wired()
        await queue.enqueue(
            RunQueueMessage(
                run_id="root-1",
                agent_id=definition.id,
                agent_version_id="version-1",
                input="hello",
            )
        )
        assert await worker.step() is True
        await _await_live(worker)
        events = [event for _cursor, event in executions.events["root-1"]]
        assert all(event.parent_run_id is None for event in events)
