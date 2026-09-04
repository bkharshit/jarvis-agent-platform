"""Repository integration: append-only versions, cursor monotonicity,
conversation sequence, transcript reconstruction."""

from __future__ import annotations

import pytest

from jarvis.domain.agent import ModelRef, StrategyConfig
from jarvis.domain.message import ToolCall
from jarvis.models.mock import turn

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
