"""Repository integration: append-only versions, cursor monotonicity,
conversation sequence, transcript reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.domain.agent import ModelRef, StrategyConfig
from jarvis.domain.message import ToolCall, Usage
from jarvis.models.mock import turn
from jarvis.ports.queue import RunQueueMessage

pytestmark = pytest.mark.db


@pytest.mark.db
async def test_agent_versions_are_append_only(container, agent):
    agent.description = "changed"
    v2 = await container.agents.update_and_publish(agent)
    assert v2.version == 2

    v1 = await container.agents.get_version(agent.id, 1)
    assert v1 is not None and v1.snapshot.description == ""  # history untouched
    latest = await container.agents.latest_version(agent.id)
    assert latest is not None and latest.version == 2
    versions = await container.agents.list_versions(agent.id)
    assert [v.version for v in versions] == [1, 2]

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await container.agents.create(agent)  # duplicate name at the DB boundary


@pytest.mark.db
async def test_event_cursors_are_monotonic_and_sequences_gapless(container, agent, mock):
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "1+2"})])
    )
    mock.add_turn(turn("The answer is 3"))

    from jarvis.domain.execution import ExecutionContext
    from tests.integration.conftest import TEST_DB_URL  # noqa: F401 — env already set

    ctx = ExecutionContext(
        run_id="run-cursor",
        agent_id=agent.id,
        agent_version_id="unused",
        trace_id="t",
    )
    version = await container.agents.latest_version(agent.id)
    assert version is not None
    await container.runtime.run(version, "what is 1+2?", ctx)

    cursors: list[int] = []
    sequences: list[int | None] = []
    async for cursor, event in container.executions.replay_with_cursor("run-cursor"):
        cursors.append(cursor)
        sequences.append(event.sequence)
    assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors)
    assert sequences == list(range(len(sequences)))  # per-run gapless


@pytest.mark.db
async def test_conversation_history_sequence(container, mock):
    from uuid import uuid4

    from jarvis.domain.agent import AgentDefinition, MemoryConfig

    definition = AgentDefinition(
        id=str(uuid4()),
        name="memory-agent",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        memory=MemoryConfig(enabled=True),
    )
    await container.agents.create(definition)
    version = await container.agents.latest_version(definition.id)
    assert version is not None
    from jarvis.domain.execution import ExecutionContext

    for i in range(2):
        ctx = ExecutionContext(
            run_id=f"run-mem-{i}",
            agent_id=definition.id,
            agent_version_id=version.id,
            session_id="s1",
            trace_id="t",
        )
        mock.add_turn(turn(f"reply {i}"))
        await container.runtime.run(version, f"question {i}", ctx)

    conversation_id = await container.conversations.get_or_create(definition.id, "s1")
    history = await container.conversations.history(conversation_id)
    roles = [m.role for m in history]
    assert roles == ["user", "assistant", "user", "assistant"]
    sequences = [m.created_at for m in history]  # ordering sanity
    assert sequences[0] <= sequences[-1]
    window = await container.conversations.history(conversation_id, limit=2)
    assert [m.text for m in window] == ["question 1", "reply 1"]


@pytest.mark.db
async def test_run_rows_reconstruct_transcript(container, agent, mock):
    mock.add_turn(
        turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "2*3"})])
    )
    mock.add_turn(turn("six"))
    from jarvis.domain.execution import ExecutionContext

    version = await container.agents.latest_version(agent.id)
    assert version is not None
    ctx = ExecutionContext(
        run_id="run-reconstruct",
        agent_id=agent.id,
        agent_version_id=version.id,
        trace_id="t",
    )
    result = await container.runtime.run(version, "compute 2*3", ctx)
    assert result.status == "succeeded"

    run = await container.executions.get("run-reconstruct")
    assert run is not None
    assert run.status == "succeeded" and run.final_message == "six"
    messages = await container.executions.list_messages("run-reconstruct")
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[-1].text == "six"
    tool_rows = await container.executions.list_tool_executions("run-reconstruct")
    assert len(tool_rows) == 1 and tool_rows[0].tool_name == "calculator"


# --- S1: run queue (ADR 0008) -------------------------------------------------


def _message(run_id: str, agent_id: str) -> RunQueueMessage:
    return RunQueueMessage(
        run_id=run_id, agent_id=agent_id, agent_version_id="v1", input="hi"
    )


@pytest.mark.db
async def test_claim_is_exactly_once_and_ordered(container, agent):
    from datetime import timedelta

    queue = container.queue
    for i in range(3):
        await queue.enqueue(_message(f"q-{i}", agent.id))

    seen = []
    for _ in range(3):
        message = await queue.claim("worker-a", timedelta(seconds=30))
        assert message is not None
        seen.append(message.run_id)
    assert await queue.claim("worker-a", timedelta(seconds=30)) is None
    assert seen == ["q-0", "q-1", "q-2"]  # FIFO by enqueue order

    # A second worker cannot claim what is already claimed — the queue is
    # drained — and requeueing hands the message back for the next claim.
    await queue.requeue("q-1")
    message = await queue.claim("worker-b", timedelta(seconds=30))
    assert message is not None and message.run_id == "q-1"


@pytest.mark.db
async def test_renew_only_extends_own_lease(container, agent):
    from datetime import timedelta

    queue = container.queue
    await queue.enqueue(_message("q-renew", agent.id))
    await queue.claim("worker-a", timedelta(seconds=30))
    assert await queue.renew("q-renew", "worker-a", timedelta(seconds=30)) is True
    assert await queue.renew("q-renew", "worker-b", timedelta(seconds=30)) is False


@pytest.mark.db
async def test_sweep_returns_only_expired_leases(container, agent):
    from datetime import UTC, datetime, timedelta

    queue = container.queue
    await queue.enqueue(_message("q-fresh", agent.id))
    await queue.enqueue(_message("q-stale", agent.id))
    await queue.claim("worker-a", timedelta(seconds=30))  # q-fresh
    # Claim the stale one, then backdate its lease under the worker's feet.
    await queue.claim("worker-a", timedelta(seconds=30))  # q-stale
    from sqlalchemy import update

    from jarvis.persistence.models import RunQueueRow

    async with container.engine.begin() as conn:
        await conn.execute(
            update(RunQueueRow)
            .where(RunQueueRow.run_id == "q-stale")
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )

    expired = await queue.sweep(datetime.now(UTC))
    assert expired == ["q-stale"]


@pytest.mark.db
async def test_cancel_requests_are_idempotent_and_popped_once(container, agent):
    queue = container.queue
    await queue.request_cancel("q-cancel", "user asked")
    await queue.request_cancel("q-cancel", "second call")  # conflict → no-op
    assert await queue.pending_cancel("q-cancel") == "user asked"
    assert await queue.pending_cancel("q-cancel") is None  # popped exactly once


@pytest.mark.db
async def test_create_queued_run_writes_row_and_message_atomically(container, agent):
    from jarvis.domain.execution import RunResult

    run_id = "q-atomic"
    result = RunResult(
        run_id=run_id, agent_id=agent.id, status="queued", input="hi", agent_version_id="v1"
    )
    await container.executions.create_queued_run(result, _message(run_id, agent.id))

    run = await container.executions.get(run_id)
    assert run is not None and run.status == "queued"
    message = await container.queue.claim("worker-a", timedelta(seconds=30))
    assert message is not None and message.run_id == run_id


@pytest.mark.db
async def test_mark_running_flips_queued_row(container, agent):
    from datetime import UTC, datetime

    from jarvis.domain.execution import RunResult

    run_id = "q-mark"
    await container.executions.create_queued_run(
        RunResult(
            run_id=run_id, agent_id=agent.id, status="queued", input="hi",
            agent_version_id="v1",
        ),
        _message(run_id, agent.id),
    )
    await container.executions.mark_running(run_id, datetime.now(UTC))
    run = await container.executions.get(run_id)
    assert run is not None and run.status == "running"
    # Already-running rows are untouched (re-claim of a requeued run).
    await container.executions.mark_running(run_id, datetime.now(UTC))
    assert (await container.executions.get(run_id)).status == "running"


@pytest.mark.db
async def test_event_sequence_helpers(container, agent, mock):
    from jarvis.domain.events import RunFailed, RunStarted

    assert await container.executions.next_event_sequence("seq-run") == 0
    assert await container.executions.count_events("seq-run") == 0

    from jarvis.events.bus import InProcessEventSink

    sink = InProcessEventSink("seq-run", persist=container.executions.append_event)
    await sink.append(
        RunStarted(
            event_id="e1",
            run_id="seq-run",
            created_at=datetime.now(UTC),
            agent_id=agent.id,
            agent_version_id="v1",
        )
    )
    assert await container.executions.next_event_sequence("seq-run") == 1
    assert await container.executions.count_events("seq-run") == 1

    latest = await container.executions.latest_event("seq-run")
    assert latest is not None
    cursor, event = latest
    assert isinstance(event, RunStarted) and event.sequence == 0

    # The sweeper's exactly-one-terminal append: next sequence, then append.
    sequence = await container.executions.next_event_sequence("seq-run")
    failed = RunFailed(
        event_id="e2",
        run_id="seq-run",
        created_at=datetime.now(UTC),
        error="worker lost",
        error_kind="model",
        total_usage=Usage(),
    )
    failed.sequence = sequence
    cursor = await container.executions.append_event(failed)
    assert await container.executions.next_event_sequence("seq-run") == 2
    latest = await container.executions.latest_event("seq-run")
    assert latest is not None and latest[1].type == "run.failed" and latest[0] == cursor
