"""Human-in-the-loop resume end to end (S10, ADR 0010) over the real queue,
worker and Postgres: a gated tool pauses the run; `enqueue_resume` merges
the answer into the acked payload; the worker's resume branch continues the
run from the durable log's next sequence to a terminal state.

Runs are driven through the queue directly: the blocking `/run` route still
waits for a TERMINAL row — it learns to stop at a pause in commit 7 (the
API seam), so pausing it here would 500 by design."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig, ToolBinding
from jarvis.domain.auth import Principal
from jarvis.domain.execution import RunResult
from jarvis.domain.message import ToolCall
from jarvis.models.mock import MockModelProvider, MockTurn
from jarvis.ports.queue import ResumeRequest, RunQueueMessage

pytestmark = pytest.mark.db

AWAITING = {"awaiting_input"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


async def _await_status(executions, run_id: str, statuses: set[str], timeout_s: float = 10.0):
    """Poll the run row until it reaches one of `statuses` (D5: it will)."""
    for _ in range(int(timeout_s / 0.1)):
        row = await executions.get(run_id)
        if row is not None and row.status in statuses:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} never reached {statuses}")


def _gated_agent(name: str) -> AgentDefinition:
    return AgentDefinition(
        id=str(uuid4()),
        name=name,
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
        tools=[ToolBinding(name="calculator", config={"requires_approval": True})],
    )


def _tool_call_turn() -> MockTurn:
    return MockTurn(
        content="",
        tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})],
    )


async def _enqueue_gated_run(
    container, agent: AgentDefinition, run_id: str, input: str = "compute"
) -> RunQueueMessage:
    """Enqueue the run the way the API's run route does (S1, ADR 0008) —
    execution row + queue message in ONE transaction."""
    version = await container.agents.latest_version(agent.id)
    message = RunQueueMessage(
        run_id=run_id, agent_id=agent.id, agent_version_id=version.id, input=input
    )
    await container.executions.create_queued_run(
        RunResult(
            run_id=run_id,
            agent_id=agent.id,
            status="queued",
            input=input,
            agent_version_id=version.id,
        ),
        message,
    )
    return message


@pytest.mark.db
async def test_pause_then_approved_resume_completes(container, mock: MockModelProvider):
    mock.add_turn(_tool_call_turn())
    mock.add_turn(MockTurn(content="42 it is", tool_calls=[]))

    agent = _gated_agent("gate-agent")
    await container.agents.create(agent)
    run_id = f"hl-{uuid4().hex[:8]}"
    await _enqueue_gated_run(container, agent, run_id)

    paused = await _await_status(container.executions, run_id, AWAITING)
    assert paused.finished_at is None  # a pause is not a finish
    # the pause event is durable and carries the gated call
    events = [e async for e in container.executions.list_events(run_id)]
    assert events[-1].type == "run.awaiting_input"
    assert [c.id for c in events[-1].pending_calls] == ["c1"]
    assert "tool.call.started" not in [e.type for e in events]
    first_segment_len = len(events)

    # the human approves — merged into the acked payload, requeued
    await container.queue.enqueue_resume(run_id, ResumeRequest(kind="tool_approval", approved=True))
    done = await _await_status(container.executions, run_id, TERMINAL)
    assert done.status == "succeeded"
    assert done.final_message == "42 it is"

    # the resumed segment continued the gapless sequence: no restart, and
    # the approved call executed in the second segment
    events = [e async for e in container.executions.list_events(run_id)]
    assert [e.sequence for e in events] == list(range(len(events)))
    resumed_types = [e.type for e in events[first_segment_len:]]
    assert "tool.call.started" in resumed_types
    assert "run.started" not in resumed_types
    assert resumed_types[-1] == "run.completed"

    # a late duplicate resume is absorbed harmlessly (stale guard)
    before = len(events)
    await container.queue.enqueue_resume(run_id, ResumeRequest(kind="tool_approval", approved=True))
    await asyncio.sleep(1.0)  # give the worker a chance to claim it
    after = [e async for e in container.executions.list_events(run_id)]
    assert len(after) == before  # no events — acked and skipped
    row = await container.executions.get(run_id)
    assert row is not None and row.status == "succeeded"


@pytest.mark.db
async def test_refused_resume_continues_with_refusal_message(container, mock: MockModelProvider):
    mock.add_turn(_tool_call_turn())
    mock.add_turn(MockTurn(content="understood, skipping", tool_calls=[]))

    agent = _gated_agent("refuse-agent")
    await container.agents.create(agent)
    run_id = f"hl-{uuid4().hex[:8]}"
    await _enqueue_gated_run(container, agent, run_id, input="x")
    await _await_status(container.executions, run_id, AWAITING)

    await container.queue.enqueue_resume(
        run_id, ResumeRequest(kind="tool_approval", approved=False)
    )
    done = await _await_status(container.executions, run_id, TERMINAL)
    assert done.status == "succeeded"

    messages = await container.executions.list_messages(run_id)
    refused = [m for m in messages if m.tool_call_id == "c1"]
    assert refused and "declined" in refused[0].content


@pytest.mark.db
async def test_sweeper_reaps_expired_pause(container, mock: MockModelProvider):
    """S10 (ADR 0010 §6): a pause whose deadline passed is finished by the
    sweeper — one terminal `run.cancelled` at the next sequence, from any
    worker; a resume racing the reap is absorbed by the stale-guard."""
    from sqlalchemy import update

    from jarvis.persistence.models import AgentExecutionRow

    mock.add_turn(_tool_call_turn())

    agent = _gated_agent("reap-agent")
    await container.agents.create(agent)
    run_id = f"hl-reap-{uuid4().hex[:8]}"
    await _enqueue_gated_run(container, agent, run_id)
    await _await_status(container.executions, run_id, AWAITING)
    before = [e async for e in container.executions.list_events(run_id)]

    # Backdate the pause deadline under the sweeper's feet.
    async with container.engine.begin() as conn:
        await conn.execute(
            update(AgentExecutionRow)
            .where(AgentExecutionRow.id == run_id)
            .values(awaiting_until=datetime.now(UTC) - timedelta(seconds=1))
        )

    acted = await container.worker.sweep()
    assert run_id in acted

    row = await container.executions.get(run_id)
    assert row.status == "cancelled" and row.finished_at is not None
    events = [e async for e in container.executions.list_events(run_id)]
    assert events[-1].type == "run.cancelled"
    assert events[-1].reason == "awaiting_input timeout"
    assert [e.sequence for e in events] == list(range(len(events)))  # gapless
    assert sum(e.type == "run.cancelled" for e in events) == 1  # exactly one terminal
    assert len(events) == len(before) + 1

    # A pending resume racing the reap is absorbed (no events, row untouched).
    await container.queue.enqueue_resume(run_id, ResumeRequest(kind="content", content="late"))
    await asyncio.sleep(1.0)
    after = [e async for e in container.executions.list_events(run_id)]
    assert len(after) == len(events)
    assert (await container.executions.get(run_id)).status == "cancelled"


@pytest.mark.db
async def test_sweeper_leaves_live_pauses_alone(container, mock: MockModelProvider):
    mock.add_turn(_tool_call_turn())

    agent = _gated_agent("live-agent")
    await container.agents.create(agent)
    run_id = f"hl-live-{uuid4().hex[:8]}"
    await _enqueue_gated_run(container, agent, run_id)
    await _await_status(container.executions, run_id, AWAITING)
    before = [e async for e in container.executions.list_events(run_id)]

    acted = await container.worker.sweep()

    assert run_id not in acted
    row = await container.executions.get(run_id)
    assert row.status == "awaiting_input"
    after = [e async for e in container.executions.list_events(run_id)]
    assert len(after) == len(before)


@pytest.mark.db
async def test_enqueue_resume_merges_and_preserves_enqueue_time_fields(container, agent):
    """The payload is the run's trust boundary: the resume MERGES into it,
    so the enqueue-time principal and deadline survive the cross-process
    hop (ADR 0010 §4)."""
    queue = container.queue
    run_id = f"q-resume-{uuid4().hex[:8]}"
    message = RunQueueMessage(
        run_id=run_id,
        agent_id=agent.id,
        agent_version_id="v1",
        input="hi",
        principal=Principal(tenant_id="t1", user_id="u1", mode="api_key"),
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    await queue.enqueue(message)
    claimed = await queue.claim("worker-a", timedelta(seconds=30))
    assert claimed is not None
    await queue.ack(run_id)  # the pause acks the original message

    await queue.enqueue_resume(run_id, ResumeRequest(kind="content", content="prod"))
    resumed = await queue.claim("worker-b", timedelta(seconds=30))
    assert resumed is not None and resumed.run_id == run_id
    assert resumed.resume == ResumeRequest(kind="content", content="prod")
    # fields NOT in the resume survive the merge
    assert resumed.principal == message.principal
    assert resumed.deadline == message.deadline
    assert resumed.input == message.input
