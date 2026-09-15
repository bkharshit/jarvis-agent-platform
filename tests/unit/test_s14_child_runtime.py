"""S14 commit 5 — non-interactive children through the REAL AgentRuntime
loop (D54/D55): tool-reported usage folds into ctx.usage, approval gates
auto-decline without pausing, AskHumanStep auto-answers. Only the model
provider and the execution repo are fakes (the test_agent_runtime doubles);
the loop, sink, ToolRuntime, and fold/decline/answer branches are real."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import is_terminal
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.runtime.agent_runtime import (
    _AUTO_ANSWER,
    _AUTO_DECLINE_COPY,
    _USER_DECLINE_COPY,
    AgentRuntime,
)
from jarvis.runtime.limits import RunLimits
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.base import BaseTool
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from tests.unit.test_agent_runtime import _AskHumanStrategy, _RecordingRepo

# --- the doubles ---------------------------------------------------------------


class _UsageReportingTool(BaseTool):
    """Reports an arbitrary usage through the S14 ToolContext channel."""

    def __init__(self, usage: dict[str, Any]):
        super().__init__(
            ToolDescriptor(
                name="usage_reporter",
                description="reports usage",
                parameters={"type": "object", "properties": {}},
            )
        )
        self._usage = usage

    async def _execute(self, arguments: dict[str, Any], context: Any) -> str:
        context.result_metadata["usage"] = dict(self._usage)
        return "reported"


def _agent(**overrides) -> AgentDefinition:
    base = dict(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        system_prompt="You are a test agent.",
    )
    base.update(overrides)
    return AgentDefinition(**base)


def _version(agent: AgentDefinition) -> AgentVersion:
    return AgentVersion(id=str(uuid4()), agent_id=agent.id, version=1, snapshot=agent)


def _ctx(run_id: str, **kw) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, agent_id="a1", agent_version_id="v1", **kw)


def _runtime(
    provider: MockModelProvider,
    tools=(),
    repo: _RecordingRepo | None = None,
    strategies: DefaultStrategyRegistry | None = None,
    **kw,
) -> AgentRuntime:
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        strategies=strategies or DefaultStrategyRegistry(),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        models=DefaultModelProviderFactory(mock_provider=provider),
        conversations=repo,
        executions=repo,
        **kw,
    )


# --- the usage fold (D54) --------------------------------------------------------


class TestUsageFold:
    async def test_tool_reported_usage_folds_and_token_budget_fires(self):
        """The child's usage folds into ctx.usage right after execute — the
        token_budget limit fires at the NEXT checkpoint (the second model
        invocation is never reached), and the terminal carries the folded
        total."""
        reporter = _UsageReportingTool({"input_tokens": 1000, "output_tokens": 500})
        provider = MockModelProvider(
            [
                turn(tool_calls=[ToolCall(id="c1", name="usage_reporter", arguments={})]),
                turn("never reached"),
            ]
        )
        repo = _RecordingRepo()
        runtime = _runtime(
            provider,
            tools=[reporter],
            repo=repo,
            limits=RunLimits(max_iterations=8, max_total_tokens=100),
        )

        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="usage_reporter")])), "go", _ctx("run-fold-1")
        )

        assert result.status == "failed"
        assert "token_budget" in (result.error or "")
        # the folded usage is on the terminal — composed with the parent's
        # own model usage (the mock turn costs 10 in / 5 out)
        assert result.total_usage.input_tokens == 1010
        assert result.total_usage.output_tokens == 505
        assert provider.invocations == 1  # the second turn was never reached
        # and the report is persisted in tool_executions (free auditability)
        assert repo.tool_executions[0][1].metadata["usage"] == {
            "input_tokens": 1000,
            "output_tokens": 500,
        }

    async def test_no_reported_usage_keeps_the_run_byte_identical(self):
        """Builtins report nothing — the budget never sees phantom tokens."""
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"})
                    ]
                ),
                turn("The result is 4"),
            ]
        )
        repo = _RecordingRepo()
        runtime = _runtime(provider, tools=[CalculatorTool()], repo=repo)
        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="calculator")])), "compute", _ctx("run-fold-2")
        )
        assert result.status == "succeeded"


# --- auto-decline approvals (D55) --------------------------------------------------


class TestAutoDecline:
    async def test_gated_call_auto_declines_without_pausing(self):
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})
                    ]
                ),
                turn("done"),
            ]
        )
        repo = _RecordingRepo()
        runtime = _runtime(provider, tools=[CalculatorTool()], repo=repo)
        ctx = _ctx("run-decline-1", metadata={"auto_decline_approvals": True})
        result = await runtime.run(
            _version(
                _agent(tools=[ToolBinding(name="calculator", config={"requires_approval": True})])
            ),
            "compute",
            ctx,
        )

        assert result.status == "succeeded"  # no pause, the run completed
        assert repo.awaited == []
        events = runtime.bus.get("run-decline-1").events
        assert [e for e in events if e.type == "tool.call.declined"]
        # the refusal copy is the child's, not the user's
        tool_messages = [m for m in repo.messages["run-decline-1"] if m.role == "tool"]
        assert tool_messages[0].content == _AUTO_DECLINE_COPY
        assert [e.type for e in events if is_terminal(e)] == ["run.completed"]

    async def test_the_human_decline_copy_is_unchanged(self):
        """The parameterization split: the USER copy stays byte-identical."""
        assert _USER_DECLINE_COPY == (
            "The user declined this tool call, so it did not run. "
            "Do not call this tool again for this request — "
            "continue without it and answer from what you know."
        )


# --- auto-answered AskHumanStep (D55) -----------------------------------------------


class TestAutoAnswer:
    async def test_ask_human_step_auto_answers_without_pausing(self):
        repo = _RecordingRepo()
        strategy = _AskHumanStrategy("Which environment?", asks=2)
        runtime = _runtime(
            MockModelProvider([]),  # the fixture never invokes the model
            repo=repo,
            strategies=DefaultStrategyRegistry(extra={"ask_human_fixture": strategy}),
        )
        ctx = _ctx("run-ask-1", metadata={"auto_decline_approvals": True})
        result = await runtime.run(
            _version(_agent(strategy=StrategyConfig(type="ask_human_fixture"))), "deploy", ctx
        )

        assert result.status == "succeeded"  # no pause — the loop kept going
        assert repo.awaited == []
        # the fixture finishes with the newest user message: the auto-answer
        assert "proceed from what you know" in (result.final_message or "")
        messages = repo.messages["run-ask-1"]
        auto_answers = [
            m
            for m in messages
            if m.role == "user" and "proceed from what you know" in (m.content or "")
        ]
        assert len(auto_answers) == 2  # asked twice, answered each time
        assert auto_answers[0].content == _AUTO_ANSWER
        events = runtime.bus.get("run-ask-1").events
        assert [e.type for e in events if is_terminal(e)] == ["run.completed"]
