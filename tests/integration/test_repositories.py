"""Repository integration: append-only versions, cursor monotonicity,
conversation sequence, transcript reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jarvis.domain.agent import ModelRef, StrategyConfig
from jarvis.domain.evaluation import EvalCase, EvalDataset, Score
from jarvis.domain.execution import RunResult
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
    return RunQueueMessage(run_id=run_id, agent_id=agent_id, agent_version_id="v1", input="hi")


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
            run_id=run_id,
            agent_id=agent.id,
            status="queued",
            input="hi",
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


@pytest.mark.db
async def test_awaiting_input_pause_transitions(container, agent):
    """S10 repo seams: running → awaiting_input (guarded), the reaper's
    expired scan, resume claim flips back (mark_running clears the
    deadline), finish clears it too."""
    from jarvis.domain.execution import RunResult

    def paused(run_id: str) -> RunResult:
        return RunResult(run_id=run_id, agent_id=agent.id, agent_version_id="v1", status="running")

    now = datetime.now(UTC)
    past = now - timedelta(hours=1)
    repo = container.executions

    # Pause: running → awaiting_input with the deadline on the row, plus
    # the chain's usage-so-far (the resume segment re-seeds its budget from
    # the row — the pause must write it).
    await repo.create_run(paused("run-hil-1"))
    await repo.mark_awaiting_input(
        "run-hil-1", past, total_usage=Usage(input_tokens=10, output_tokens=4)
    )
    paused_row = await repo.get("run-hil-1")
    assert paused_row.status == "awaiting_input"
    assert paused_row.total_usage.input_tokens == 10
    assert "run-hil-1" in await repo.expired_awaiting(now)

    # The guard: a non-running row never pauses (no terminal or queued run
    # can drift into awaiting_input through a raced claim).
    await repo.create_run(paused("run-hil-2"))
    await repo.finish_run(
        RunResult(
            run_id="run-hil-2",
            agent_id=agent.id,
            agent_version_id="v1",
            status="cancelled",
        )
    )
    await repo.mark_awaiting_input("run-hil-2", past)
    assert (await repo.get("run-hil-2")).status == "cancelled"

    # Resume claim: awaiting_input → running, deadline cleared.
    await repo.mark_running("run-hil-1", now)
    resumed = await repo.get("run-hil-1")
    assert resumed.status == "running"
    assert "run-hil-1" not in await repo.expired_awaiting(now)

    # Finish: terminal clears the deadline too.
    await repo.create_run(paused("run-hil-3"))
    await repo.mark_awaiting_input("run-hil-3", past)
    await repo.finish_run(
        RunResult(
            run_id="run-hil-3",
            agent_id=agent.id,
            agent_version_id="v1",
            status="succeeded",
        )
    )
    assert (await repo.get("run-hil-3")).status == "succeeded"
    assert await repo.expired_awaiting(now) == []


@pytest.mark.db
async def test_conversation_summary_state_roundtrip(container, agent):
    """S12 (D45): summary state persists on the conversation row; tenant
    scoping follows the D29 absence rule (foreign tenant reads None)."""
    from uuid import uuid4

    from jarvis.domain.agent import AgentDefinition

    definition = AgentDefinition(
        id=str(uuid4()),
        name="summary-state-agent",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
    )
    await container.agents.create(definition)
    conversation_id = await container.conversations.get_or_create(definition.id, "s-summary")

    state = await container.conversations.get_summary_state(conversation_id)
    assert state is not None
    assert state.summary is None and state.summarized_count == 0

    await container.conversations.save_summary(
        conversation_id, summary="the user asked about quotas", summarized_count=4
    )
    state = await container.conversations.get_summary_state(conversation_id)
    assert state is not None
    assert state.summary == "the user asked about quotas"
    assert state.summarized_count == 4

    # overwriting rolls the state forward (compaction is rolling, not append)
    await container.conversations.save_summary(
        conversation_id, summary="quotas then billing", summarized_count=9
    )
    state = await container.conversations.get_summary_state(conversation_id)
    assert state is not None and state.summarized_count == 9

    # a foreign tenant sees absence, not the state (D29)
    foreign = await container.conversations.get_summary_state(conversation_id, tenant_id="t-other")
    assert foreign is None

    # unknown conversation reads as absent
    assert await container.conversations.get_summary_state("no-such-conversation") is None


@pytest.mark.db
async def test_scratchpad_upsert_and_roundtrip(container):
    """S12 (D46): the working-memory store upserts on its natural key
    (agent_id, session_id, key) and reads back what was written."""
    store = container.scratchpad
    entry = await store.put("agent-scr", "s1", "notes", "first draft")
    assert entry.value == "first draft"
    assert entry.agent_id == "agent-scr" and entry.key == "notes"

    fetched = await store.get("agent-scr", "s1", "notes")
    assert fetched is not None and fetched.value == "first draft"

    # upsert: the newest write wins, still one row for the key
    updated = await store.put("agent-scr", "s1", "notes", "second draft")
    assert updated.value == "second draft"
    assert updated.updated_at >= entry.updated_at
    fetched = await store.get("agent-scr", "s1", "notes")
    assert fetched is not None and fetched.value == "second draft"

    # keys, sessions, and agents do not bleed into each other
    assert await store.get("agent-scr", "s1", "other-key") is None
    assert await store.get("agent-scr", "s2", "notes") is None
    assert await store.get("agent-other", "s1", "notes") is None


@pytest.mark.db
async def test_scratchpad_tenant_scoping_and_delete(container):
    """S12 (D46/D29): foreign-tenant reads are absence, deletes are scoped,
    and deleting an unset key is False, not an error."""
    store = container.scratchpad
    await store.put("agent-scr", "s1", "secret", "value")
    await store.put("agent-scr", "s1", "plain", "value")

    # a foreign tenant reads absence (D29) — never the row
    assert await store.get("agent-scr", "s1", "secret", tenant_id="t-other") is None
    # a scoped delete touches nothing; the row survives
    assert await store.delete("agent-scr", "s1", "secret", tenant_id="t-other") is False
    assert (await store.get("agent-scr", "s1", "secret")) is not None

    # default-tenant writes read back through the explicit tenant
    assert (await store.get("agent-scr", "s1", "secret", tenant_id="default")) is not None

    # delete removes exactly the one key
    assert await store.delete("agent-scr", "s1", "secret") is True
    assert await store.get("agent-scr", "s1", "secret") is None
    assert await store.get("agent-scr", "s1", "plain") is not None
    assert await store.delete("agent-scr", "s1", "secret") is False


# --- S11: evaluation datasets / runs / results (ADR 0017, D48) ----------


def _eval_dataset(name: str = "smoke", **overrides: object) -> EvalDataset:
    base: dict[str, object] = {
        "id": f"ds-{name}",
        "name": name,
        "cases": [
            {"id": "c-1", "input": "what is 2+2?", "expected": "4"},
            {"id": "c-2", "input": "capital of France?", "expected": "Paris"},
        ],
        "scorers": [{"name": "exact"}],
    }
    base.update(overrides)
    return EvalDataset.model_validate(base)


def _child_run(run_id: str, agent_id: str) -> RunResult:
    """A queued child-run row for the eval_results FK (D48: run_ids exist
    before the eval-run transaction)."""
    return RunResult(
        run_id=run_id,
        agent_id=agent_id,
        status="queued",
        input="q",
        agent_version_id="ver-1",
        tenant_id="default",
        session_id=None,
        trace_id="t-eval",
        metadata={},
    )


@pytest.mark.db
async def test_eval_dataset_roundtrip_and_update(container):
    repo = container.evaluations
    created = await repo.create_dataset(_eval_dataset())
    assert created.id == "ds-smoke" and len(created.cases) == 2

    fetched = await repo.get_dataset("ds-smoke")
    assert fetched is not None
    assert fetched == created

    listed = await repo.list_datasets()
    assert [d.id for d in listed if d.id == "ds-smoke"] == ["ds-smoke"]

    # update replaces the mutable fields wholesale
    updated = await repo.update_dataset(
        _eval_dataset(
            "smoke",
            description="v2",
            scorers=[{"name": "exact"}, {"name": "llm_judge"}],
            judge_model={"provider": "mock", "model": "mock-1"},
        )
    )
    assert updated is not None
    assert updated.description == "v2"
    assert [s.name for s in updated.scorers] == ["exact", "llm_judge"]
    assert updated.judge_model == ModelRef(provider="mock", model="mock-1")
    assert updated.updated_at >= updated.created_at


@pytest.mark.db
async def test_eval_dataset_tenant_scoping(container):
    """S11 (D29): foreign-tenant reads are absence; scoped mutations touch
    nothing; default-tenant rows read through the explicit tenant."""
    repo = container.evaluations
    await container.auth.create_tenant("t-other", "Other")  # FK target for the foreign write
    await repo.create_dataset(_eval_dataset("tenanted"))

    assert await repo.get_dataset("ds-tenanted", tenant_id="t-other") is None
    assert await repo.get_dataset("ds-tenanted", tenant_id="default") is not None
    assert await repo.update_dataset(_eval_dataset("tenanted"), tenant_id="t-other") is None
    assert await repo.delete_dataset("ds-tenanted", tenant_id="t-other") is False
    assert await repo.get_dataset("ds-tenanted") is not None

    # tenant-scoped listings exclude other tenants' rows
    other = await repo.create_dataset(_eval_dataset("foreign"), tenant_id="t-other")
    assert [d.id for d in await repo.list_datasets(tenant_id="default")] == ["ds-tenanted"]
    assert [d.id for d in await repo.list_datasets(tenant_id="t-other")] == ["ds-foreign"]
    assert other.tenant_id if hasattr(other, "tenant_id") else True  # domain type is tenant-blind


@pytest.mark.db
async def test_eval_run_snapshot_results_and_persist_once(container):
    """S11 (D48/D49): create_run snapshots the dataset, references the
    children eagerly in ONE transaction, and save_scores persists ONCE —
    the scores-IS-NULL guard makes a later call a no-op."""
    repo = container.evaluations
    dataset = await repo.create_dataset(_eval_dataset("runs"))
    children = [("run-eval-1", "c-1"), ("run-eval-2", "c-2")]
    for run_id, _ in children:
        await container.executions.create_run(_child_run(run_id, "agent-eval"))

    run = await repo.create_run(
        dataset,
        "agent-eval",
        "ver-1",
        [(EvalCase(id=cid, input="q"), rid) for rid, cid in children],
    )
    assert run.dataset == dataset  # the snapshot rides the domain type

    fetched = await repo.get_run(run.id)
    assert fetched is not None
    assert fetched.dataset == dataset  # snapshot persisted, not a reference

    results = await repo.get_results(run.id)
    assert [r.case_id for r in results] == ["c-1", "c-2"]
    assert all(r.scores is None and r.scored_at is None for r in results)

    # lazy scoring persists once; a later call must NOT overwrite (D49)
    scores = [Score(scorer="exact", passed=True, score=1.0, detail="match")]
    await repo.save_scores(run.id, "c-1", scores, None)
    first = (await repo.get_results(run.id))[0]
    assert first.scores is not None and first.scored_at is not None

    await repo.save_scores(run.id, "c-1", [Score(scorer="exact", passed=False)], "rewritten?")
    again = (await repo.get_results(run.id))[0]
    assert again.scores == first.scores and again.scored_at == first.scored_at

    # a dataset with runs refuses deletion (caller maps to 409)
    assert await repo.delete_dataset("ds-runs") is False
    assert await repo.get_dataset("ds-runs") is not None


@pytest.mark.db
async def test_eval_version_scores_groups_results(container):
    """S11: list_version_scores returns every eval run of an agent with
    its results — the comparison query groups by agent_version_id."""
    repo = container.evaluations
    dataset = await repo.create_dataset(_eval_dataset("compare"))
    await container.auth.create_tenant("t-other", "Other")  # FK target for the foreign run
    for run_id in ("run-v1", "run-v2"):
        await container.executions.create_run(_child_run(run_id, "agent-cmp"))

    await repo.create_run(
        dataset, "agent-cmp", "ver-1", [(EvalCase(id="c-1", input="q"), "run-v1")]
    )
    await repo.create_run(
        dataset,
        "agent-cmp",
        "ver-2",
        [(EvalCase(id="c-1", input="q"), "run-v2")],
        tenant_id="t-other",
    )

    pairs = await repo.list_version_scores("agent-cmp")
    assert [(run.agent_version_id, len(results)) for run, results in pairs] == [
        ("ver-1", 1),
        ("ver-2", 1),
    ]
    # tenant scoping hides the foreign run
    scoped = await repo.list_version_scores("agent-cmp", tenant_id="default")
    assert [run.agent_version_id for run, _ in scoped] == ["ver-1"]
