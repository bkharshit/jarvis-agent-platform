"""Domain model validation: messages, usage, agents, execution state."""

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.execution import (
    CancellationToken,
    ExecutionCancelled,
    ExecutionContext,
    RunResult,
)
from jarvis.domain.message import Message, TextPart, Usage, assistant, tool_result


class TestUsage:
    def test_plus_accumulates(self):
        a = Usage(input_tokens=1, output_tokens=2, extra={"cached": 3})
        b = Usage(input_tokens=10, output_tokens=20, extra={"cached": 4})
        total = a.plus(b)
        assert total.input_tokens == 11
        assert total.output_tokens == 22
        assert total.extra == {"cached": 7}

    def test_plus_returns_new_instance(self):
        a = Usage(input_tokens=1)
        total = a.plus(a)
        assert a.input_tokens == 1
        assert total.input_tokens == 2


class TestMessage:
    def test_text_from_string(self):
        assert Message(role="user", content="hello").text == "hello"

    def test_text_from_parts(self):
        msg = Message(role="user", content=[TextPart(text="a"), TextPart(text="b")])
        assert msg.text == "ab"

    def test_empty_content_defaults(self):
        msg = assistant()
        assert msg.text == ""
        assert msg.tool_calls is None

    def test_tool_result_helper(self):
        msg = tool_result("tc1", "calculator", "42")
        assert msg.role == "tool"
        assert msg.tool_call_id == "tc1"
        assert msg.name == "calculator"

    def test_forbids_unknown_fields(self):
        with pytest.raises(ValidationError):
            Message(role="user", content="x", bogus=1)


class TestAgentDefinition:
    def _definition(self, **overrides):
        base = dict(
            id="a1",
            name="demo",
            model=ModelRef(provider="mock", model="mock-1"),
            strategy=StrategyConfig(type="function_calling"),
        )
        base.update(overrides)
        return AgentDefinition(**base)

    def test_minimal_definition(self):
        agent = self._definition()
        assert agent.max_iterations == 8
        assert agent.temperature == 0.7
        assert agent.memory.enabled is False
        assert agent.tools == []

    def test_blank_name_rejected(self):
        with pytest.raises(ValidationError):
            self._definition(name="   ")

    def test_max_iterations_bounds(self):
        with pytest.raises(ValidationError):
            self._definition(max_iterations=0)
        with pytest.raises(ValidationError):
            self._definition(max_iterations=33)

    def test_strategy_type_limited(self):
        with pytest.raises(ValidationError):
            self._definition(strategy=StrategyConfig(type="bogus"))

    def test_enabled_tools_filters(self):
        agent = self._definition(
            tools=[
                ToolBinding(name="calc", enabled=True),
                ToolBinding(name="off", enabled=False),
            ]
        )
        assert [t.name for t in agent.enabled_tools()] == ["calc"]

    def test_version_snapshot_is_full_definition(self):
        agent = self._definition()
        version = AgentVersion(id="v1", agent_id="a1", version=1, snapshot=agent)
        assert version.snapshot.name == "demo"
        assert version.snapshot.model_dump() == agent.model_dump()


class TestCancellationToken:
    def test_trigger_then_raise(self):
        token = CancellationToken()
        assert not token.triggered
        token.trigger("user request")
        assert token.triggered
        with pytest.raises(ExecutionCancelled, match="user request"):
            token.raise_if_triggered()

    def test_first_reason_wins(self):
        token = CancellationToken()
        token.trigger("first")
        token.trigger("second")
        assert token.reason == "first"

    def test_child_fires_with_parent(self):
        parent = CancellationToken()
        child = parent.child()
        parent.trigger("stop")
        assert child.triggered
        assert child.reason == "stop"

    def test_child_can_fire_alone(self):
        parent = CancellationToken()
        child = parent.child()
        child.trigger("tool timeout")
        assert child.triggered
        assert not parent.triggered

    def test_child_created_after_trigger_fires_immediately(self):
        parent = CancellationToken()
        parent.trigger("done")
        assert parent.child().triggered


class TestExecutionContext:
    def test_check_limits_passes_when_clear(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v")
        ctx.check_limits()

    def test_check_limits_raises_when_cancelled(self):
        ctx = ExecutionContext(run_id="r", agent_id="a", agent_version_id="v")
        ctx.cancel.trigger("bye")
        with pytest.raises(ExecutionCancelled):
            ctx.check_limits()

    def test_check_limits_raises_past_deadline(self):
        from datetime import UTC, timedelta
        from datetime import datetime as dt

        ctx = ExecutionContext(
            run_id="r",
            agent_id="a",
            agent_version_id="v",
            deadline=dt.now(UTC) - timedelta(seconds=1),
        )
        with pytest.raises(ExecutionCancelled, match="deadline"):
            ctx.check_limits()


class TestRunResult:
    def test_defaults(self):
        result = RunResult(run_id="r", agent_id="a", status="succeeded")
        assert result.final_message is None
        assert result.iterations == 0
        assert result.event_cursor is None
        assert result.finished_at is None
