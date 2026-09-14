"""WorkflowRuntime with fakes (S6, ADR 0015 §5): the sequential walk over
acyclic graphs — linear chains, condition routing, tool nodes, the
node cap, inner-failure terminals naming the node, cancel/deadline
mid-walk, never-raises resolution failures, and usage accumulation under
the shared token budget. Agent-node segments ride a fake SegmentRunner;
the real AgentRuntime loop is the agent suite's business. The worker's
`kind` branch is covered in test_worker.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import NodeCompleted, NodeStarted, RunAwaitingInput
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.domain.message import Usage
from jarvis.domain.workflow import (
    AgentNodeConfig,
    ConditionNodeConfig,
    ConditionOperator,
    ConditionRoute,
    ToolNodeConfig,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowVersion,
)
from jarvis.events.bus import InProcessEventSink
from jarvis.models.errors import ModelError
from jarvis.ports.queue import ResumeRequest
from jarvis.runtime.agent_runtime import LoopOutcome, PauseOutcome
from jarvis.runtime.limits import RunLimits
from jarvis.runtime.workflow_runtime import WorkflowRuntime
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.mcp.errors import McpResolutionError
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


class FakeRunner:
    """SegmentRunner over a script of LoopOutcomes (or callables receiving
    ctx — for mid-walk cancel triggering and usage writes). `resume_script`
    drives `resume_segment` separately."""

    def __init__(self, script=None, resume_script=None) -> None:
        self.calls: list[tuple[AgentVersion, str, ExecutionContext, object]] = []
        self.script = list(script or [])
        self.resume_script = list(resume_script or [])
        self.resumes: list[tuple[AgentVersion, object, int | None]] = []

    async def _run_segment(self, version, input, ctx, sink, started=None):
        self.calls.append((version, input, ctx, sink))
        if self.script:
            step = self.script.pop(0)
            if callable(step):
                return step(ctx)
            return step
        return LoopOutcome(kind="completed", final_message=f"done:{input}", iterations=1)

    async def resume_segment(self, version, run_id, ctx, sink, resume, *, open_iteration=None):
        self.resumes.append((version, resume, open_iteration))
        if self.resume_script:
            step = self.resume_script.pop(0)
            if callable(step):
                return step(ctx)
            return step
        return LoopOutcome(kind="completed", final_message="resumed", iterations=1)

    def cancel(self, run_id, reason="cancelled by user") -> bool:
        return True

    def is_live(self, run_id) -> bool:
        return False


class RecordingExecutions:
    def __init__(self) -> None:
        self.created: list[RunResult] = []
        self.finished: list[RunResult] = []
        self.marked: list[tuple[str, object]] = []
        self.tool_rows: list[tuple[str, object]] = []
        self.rows: dict[str, RunResult] = {}
        self.events: dict[str, list[Any]] = {}

    async def create_run(self, result):
        self.created.append(result)
        self.rows[result.run_id] = result

    async def finish_run(self, result):
        self.finished.append(result)
        self.rows[result.run_id] = result

    async def mark_awaiting_input(self, run_id, awaiting_until, *, total_usage=None):
        self.marked.append((run_id, awaiting_until))

    async def save_tool_execution(self, run_id, result, arguments):
        self.tool_rows.append((run_id, result))

    async def get(self, run_id: str):
        return self.rows.get(run_id)

    async def list_events(self, run_id: str):
        for event in self.events.get(run_id, []):
            yield event


def _agent(pin: str = "pin-1", agent_id: str = "agent-1") -> AgentVersion:
    return AgentVersion(
        id=pin,
        agent_id=agent_id,
        version=1,
        snapshot=AgentDefinition(
            id=agent_id,
            name="node-agent",
            model=ModelRef(provider="mock", model="m"),
            strategy=StrategyConfig(type="function_calling"),
        ),
    )


class FakeVersionLoader:
    def __init__(self, versions: dict[str, AgentVersion]) -> None:
        self._versions = versions

    async def get_version_by_id(self, version_id: str) -> AgentVersion | None:
        return self._versions.get(version_id)


def _agent_node(
    node_id: str, agent_id: str = "agent-1", pin: str = "pin-1", template: str = "{{input}}"
) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="agent",
        config=AgentNodeConfig(agent_id=agent_id, agent_version_id=pin, input_template=template),
    )


def _condition_node(node_id: str, contains: str, to_node: str, else_node: str) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="condition",
        config=ConditionNodeConfig(
            routes=[
                ConditionRoute(
                    when=ConditionOperator(operator="contains", value=contains), to_node=to_node
                )
            ],
            else_node=else_node,
        ),
    )


def _workflow(nodes, edges=(), start=None, **kw) -> WorkflowVersion:
    definition = WorkflowDefinition(
        id=str(uuid4()),
        name="wf",
        nodes=list(nodes),
        edges=list(edges),
        start_node_id=start or nodes[0].id,
        **kw,
    )
    return WorkflowVersion(id="wfv-1", workflow_id=definition.id, version=1, snapshot=definition)


def _runtime(runner, versions=None, tools=(), limits=None, executions=None, mcp=None):
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return WorkflowRuntime(
        runner=runner,
        versions=versions or FakeVersionLoader({"pin-1": _agent()}),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        executions=executions,
        limits=limits,
        mcp=mcp,
    )


def _ctx(run_id: str = "wrun-1", **kw) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, agent_id="wf-1", agent_version_id="wfv-1", **kw)


_RESUME = ResumeRequest(kind="content", content="answer")


class TestLinearChain:
    async def test_two_agent_chain_templates_upstream_output(self):
        wf = _workflow(
            [_agent_node("a"), _agent_node("b", template="{{node.a}}!")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        runner = FakeRunner()
        runtime = _runtime(runner)
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "succeeded"
        assert result.final_message == "done:done:hi!"  # node b saw node a's output
        assert [call[1] for call in runner.calls] == ["hi", "done:hi!"]
        assert result.iterations == 2
        events = runtime.bus.get("wrun-1").events
        assert [e.type for e in events] == [
            "run.started",
            "node.started",
            "node.completed",
            "node.started",
            "node.completed",
            "run.completed",
        ]
        assert events[2].node_id == "a" and events[2].output == "done:hi"
        assert events[5].final_message == "done:done:hi!"

    async def test_row_lifecycle_records_running_then_finished(self):
        wf = _workflow([_agent_node("a")])
        repo = RecordingExecutions()
        runtime = _runtime(FakeRunner(), executions=repo)
        result = await runtime.run(wf, "hi", _ctx())

        assert [r.status for r in repo.created] == ["running"]
        assert [r.status for r in repo.finished] == ["succeeded"] and repo.finished == [result]


class TestConditionRouting:
    async def test_matching_route_takes_the_branch(self):
        wf = _workflow(
            [
                _agent_node("a"),
                _condition_node("c", contains="yes", to_node="good", else_node="bad"),
                _agent_node("good", template="{{node.a}}"),
                _agent_node("bad"),
            ],
            [WorkflowEdge(from_node="a", to_node="c")],
        )
        runner = FakeRunner(
            script=[LoopOutcome(kind="completed", final_message="yes", iterations=1)]
        )
        runtime = _runtime(runner)
        result = await runtime.run(wf, "q", _ctx())

        assert result.status == "succeeded"
        # the good node ran, the bad node never did
        assert [call[0].id for call in runner.calls[1:]] == ["pin-1"]
        events = runtime.bus.get("wrun-1").events
        assert [e.node_id for e in events if e.type == "node.started"] == ["a", "c", "good"]
        assert result.final_message == "done:yes"

    async def test_no_match_takes_the_else_node(self):
        wf = _workflow(
            [
                _agent_node("a"),
                _condition_node("c", contains="yes", to_node="good", else_node="bad"),
                _agent_node("good"),
                _agent_node("bad", template="{{node.a}}"),
            ],
            [WorkflowEdge(from_node="a", to_node="c")],
        )
        runner = FakeRunner(
            script=[LoopOutcome(kind="completed", final_message="no", iterations=1)]
        )
        runtime = _runtime(runner)
        result = await runtime.run(wf, "q", _ctx())

        assert result.status == "succeeded"
        started = [e.node_id for e in runtime.bus.get("wrun-1").events if e.type == "node.started"]
        assert started == ["a", "c", "bad"]
        assert result.final_message == "done:no"


class TestToolNode:
    async def test_tool_node_executes_with_templated_arguments(self):
        wf = _workflow(
            [
                WorkflowNode(
                    id="t",
                    type="tool",
                    config=ToolNodeConfig(
                        binding=ToolBinding(name="calculator"),
                        arguments={"expression": "6*{{n}}"},
                    ),
                )
            ],
        )
        calc = CalculatorTool()
        runtime = _runtime(FakeRunner(), tools=[calc])
        ctx = _ctx(variables={"n": 7})
        result = await runtime.run(wf, "hi", ctx)

        assert result.status == "succeeded"
        assert result.final_message == "42"
        events = runtime.bus.get("wrun-1").events
        assert [e.type for e in events] == [
            "run.started",
            "node.started",
            "tool.call.requested",
            "tool.call.started",
            "tool.call.completed",
            "node.completed",
            "run.completed",
        ]
        # every node-scoped event carries the executing node's node_id
        assert all(e.node_id == "t" for e in events[1:-1])
        assert events[2].arguments == {"expression": "6*7"}  # the template rendered


class TestNodeCapAndBudget:
    async def test_node_cap_terminal_names_the_cap(self):
        wf = _workflow(
            [_agent_node("a"), _agent_node("b")],
            [WorkflowEdge(from_node="a", to_node="b")],
            max_node_executions=1,
        )
        runtime = _runtime(FakeRunner())
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "max_iterations"
        assert "max_node_executions=1" in (result.error or "")
        events = runtime.bus.get("wrun-1").events
        assert [e.type for e in events] == [
            "run.started",
            "node.started",
            "node.completed",
            "run.failed",
        ]

    async def test_token_budget_spans_the_whole_walk(self):
        def _spend(ctx):
            ctx.usage = Usage(input_tokens=ctx.usage.input_tokens + 1000, output_tokens=100)
            return LoopOutcome(kind="completed", final_message="ok", iterations=1)

        # three nodes: the walk's top check fires before node c — after node
        # b's spend pushed the shared usage past the cap (node b's own spend
        # is not re-checked, exactly like the agent loop's top checkpoint).
        wf = _workflow(
            [_agent_node("a"), _agent_node("b"), _agent_node("c")],
            [
                WorkflowEdge(from_node="a", to_node="b"),
                WorkflowEdge(from_node="b", to_node="c"),
            ],
        )
        runtime = _runtime(
            FakeRunner(script=[_spend, _spend]),
            limits=RunLimits(max_iterations=99, max_total_tokens=1500),
        )
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "max_iterations"
        assert "token_budget" in (result.error or "")

    async def test_usage_accumulates_into_the_terminal(self):
        def _spend(ctx):
            ctx.usage = Usage(input_tokens=ctx.usage.input_tokens + 1000, output_tokens=100)
            return LoopOutcome(kind="completed", final_message="ok", iterations=1)

        wf = _workflow(
            [_agent_node("a"), _agent_node("b")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        runtime = _runtime(FakeRunner(script=[_spend, _spend]))
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "succeeded"
        assert result.total_usage.input_tokens == 2000
        events = runtime.bus.get("wrun-1").events
        assert events[-1].total_usage.input_tokens == 2000


class TestInnerFailureAndCancel:
    async def test_inner_failure_terminal_names_the_node(self):
        wf = _workflow([_agent_node("a")])
        runner = FakeRunner(
            script=[LoopOutcome(kind="failed", error="model boom", error_kind="model")]
        )
        runtime = _runtime(runner)
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "model"
        assert result.error == "node 'a': model boom"
        events = runtime.bus.get("wrun-1").events
        assert events[-1].type == "run.failed"
        assert events[-1].error == "node 'a': model boom"

    async def test_cancel_mid_walk(self):
        def _trigger(ctx):
            ctx.cancel.trigger("user asked")
            return LoopOutcome(kind="completed", final_message="ok", iterations=1)

        wf = _workflow(
            [_agent_node("a"), _agent_node("b")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        runtime = _runtime(FakeRunner(script=[_trigger]))
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "cancelled"
        events = runtime.bus.get("wrun-1").events
        assert events[-1].type == "run.cancelled"
        assert events[-1].reason == "user asked"

    async def test_deadline_exceeded_is_a_timeout_terminal(self):
        wf = _workflow([_agent_node("a")])
        runtime = _runtime(FakeRunner())
        ctx = _ctx(deadline=datetime.now(UTC) - timedelta(seconds=1))
        result = await runtime.run(wf, "hi", ctx)

        assert result.status == "timed_out"
        assert result.error_kind == "timeout"
        events = runtime.bus.get("wrun-1").events
        assert events[-1].type == "run.failed"
        assert events[-1].error_kind == "timeout"


class TestNeverRaises:
    async def test_missing_pinned_version_is_a_terminal_model_failure(self):
        wf = _workflow([_agent_node("a", pin="gone")])
        runtime = _runtime(FakeRunner(), versions=FakeVersionLoader({}))
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "model"
        assert "gone" in (result.error or "")
        events = runtime.bus.get("wrun-1").events
        assert events[-1].type == "run.failed"

    async def test_unpublished_draft_pin_is_a_terminal_model_failure(self):
        definition = WorkflowDefinition(
            id=str(uuid4()),
            name="wf",
            nodes=[
                WorkflowNode(
                    id="a",
                    type="agent",
                    config=AgentNodeConfig(
                        agent_id="agent-1", agent_version_id=None, input_template="{{input}}"
                    ),
                )
            ],
            start_node_id="a",
        )
        version = WorkflowVersion(
            id="wfv-2", workflow_id=definition.id, version=1, snapshot=definition
        )
        runtime = _runtime(FakeRunner(), versions=FakeVersionLoader({}))
        result = await runtime.run(version, "hi", _ctx())

        assert result.status == "failed"
        assert "no pinned agent version" in (result.error or "")

    async def test_pin_owner_mismatch_is_a_terminal_model_failure(self):
        wf = _workflow([_agent_node("a", pin="pin-1")])
        wrong = _agent(pin="pin-1", agent_id="other-agent")
        runtime = _runtime(FakeRunner(), versions=FakeVersionLoader({"pin-1": wrong}))
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert "belongs to agent" in (result.error or "")

    async def test_mcp_resolution_failure_is_a_terminal_tool_failure(self):
        class _FailingMcp:
            async def resolve(self, bindings, *, tenant_id=None, principal=None):
                raise McpResolutionError("mcp__x", "unreachable")

        wf = _workflow([_agent_node("a")])
        runtime = _runtime(FakeRunner(), mcp=_FailingMcp())
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "tool"
        assert "mcp__x" in (result.error or "")  # the server is named

    async def test_model_error_from_resolution_is_a_terminal_model_failure(self):
        class _FailingLoader:
            async def get_version_by_id(self, version_id: str):
                raise ModelError("credential boom")

        wf = _workflow([_agent_node("a")])
        runtime = _runtime(FakeRunner(), versions=_FailingLoader())
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "failed"
        assert result.error_kind == "model"


class TestPauseInsideNode:
    async def test_pause_marks_the_row_and_returns_awaiting_input(self):
        wf = _workflow([_agent_node("a")])
        pause = PauseOutcome(
            reason="strategy",
            question="Which environment?",
            awaiting_until=datetime.now(UTC) + timedelta(hours=1),
            pause_cursor=4,
        )
        repo = RecordingExecutions()
        runtime = _runtime(
            FakeRunner(script=[LoopOutcome(kind="paused", iterations=1, pause=pause)]),
            executions=repo,
        )
        result = await runtime.run(wf, "hi", _ctx())

        assert result.status == "awaiting_input"
        assert result.event_cursor == 4
        assert len(repo.marked) == 1  # the awaiting_input row write
        assert repo.finished == []  # no terminal finish on a pause
        # no terminal event: the pause frame belongs to the inner segment
        events = runtime.bus.get("wrun-1").events
        assert events[-1].type == "node.started"
        assert "run.completed" not in [e.type for e in events]


class TestResume:
    """The S10 composition (ADR 0015 §7): resume re-enters the walk at the
    node whose pause frame the event log names."""

    def _paused_log(self, repo: RecordingExecutions, *, output_a: str = "A out") -> None:
        """A durable log like a real paused walk left it: node a completed,
        node b started and paused (the pause frame rode the node sink)."""
        run_id = "wrun-1"
        repo.rows[run_id] = RunResult(
            run_id=run_id, agent_id="wf-1", status="awaiting_input", input="hi"
        )
        repo.events[run_id] = [
            NodeStarted(
                event_id=str(uuid4()),
                run_id=run_id,
                created_at=datetime.now(UTC),
                node_id="a",
                node_type="agent",
            ),
            NodeCompleted(
                event_id=str(uuid4()),
                run_id=run_id,
                created_at=datetime.now(UTC),
                node_id="a",
                node_type="agent",
                output=output_a,
            ),
            NodeStarted(
                event_id=str(uuid4()),
                run_id=run_id,
                created_at=datetime.now(UTC),
                node_id="b",
                node_type="agent",
            ),
            RunAwaitingInput(
                event_id=str(uuid4()),
                run_id=run_id,
                created_at=datetime.now(UTC),
                reason="tool_approval",
                pending_calls=[],
                awaiting_until=datetime.now(UTC) + timedelta(hours=1),
                node_id="b",
            ),
        ]

    async def test_resume_continues_the_walk_from_the_paused_node(self):
        wf = _workflow(
            [_agent_node("a", template="{{input}}"), _agent_node("b", template="{{node.a}}!")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        repo = RecordingExecutions()
        self._paused_log(repo, output_a="A out")
        runner = FakeRunner(
            resume_script=[LoopOutcome(kind="completed", final_message="B", iterations=1)]
        )
        runtime = _runtime(runner, executions=repo)
        ctx = _ctx()
        sink = InProcessEventSink("wrun-1")
        result = await runtime.resume(wf, "wrun-1", ctx, sink, _RESUME)

        assert result.status == "succeeded"
        assert result.final_message == "B"  # the resumed node is the walk's last
        # the paused node resumed — NOT re-executed from the start
        assert [v.id for v, _r, _o in runner.resumes] == ["pin-1"] and runner.calls == []
        events = sink.events
        assert [e.type for e in events] == ["node.completed", "run.completed"]
        assert events[0].node_id == "b"  # the resumed node, not node a

    async def test_resume_then_downstream_node_runs(self):
        wf = _workflow(
            [_agent_node("a"), _agent_node("b"), _agent_node("c", template="{{node.b}}")],
            [
                WorkflowEdge(from_node="a", to_node="b"),
                WorkflowEdge(from_node="b", to_node="c"),
            ],
        )
        repo = RecordingExecutions()
        self._paused_log(repo, output_a="A out")
        runner = FakeRunner(
            resume_script=[LoopOutcome(kind="completed", final_message="B", iterations=1)],
            script=[LoopOutcome(kind="completed", final_message="C", iterations=1)],
        )
        runtime = _runtime(runner, executions=repo)
        result = await runtime.resume(wf, "wrun-1", _ctx(), InProcessEventSink("wrun-1"), _RESUME)

        assert result.status == "succeeded"
        # node c rode the fresh-walk path and saw the resumed node's output
        assert result.final_message == "C" and [c[1] for c in runner.calls] == ["B"]

    async def test_resume_can_pause_again(self):
        wf = _workflow(
            [_agent_node("a", pin="pin-1"), _agent_node("b", pin="pin-1")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        repo = RecordingExecutions()
        self._paused_log(repo)
        pause = PauseOutcome(
            reason="strategy",
            question="again?",
            awaiting_until=datetime.now(UTC) + timedelta(hours=1),
            pause_cursor=9,
        )
        runtime = _runtime(
            FakeRunner(resume_script=[LoopOutcome(kind="paused", iterations=1, pause=pause)]),
            executions=repo,
        )
        result = await runtime.resume(wf, "wrun-1", _ctx(), InProcessEventSink("wrun-1"), _RESUME)

        assert result.status == "awaiting_input"
        assert result.event_cursor == 9
        assert repo.finished == []

    async def test_resume_inner_failure_names_the_node(self):
        wf = _workflow(
            [_agent_node("a"), _agent_node("b")],
            [WorkflowEdge(from_node="a", to_node="b")],
        )
        repo = RecordingExecutions()
        self._paused_log(repo)
        runtime = _runtime(
            FakeRunner(
                resume_script=[LoopOutcome(kind="failed", error="boom", error_kind="strategy")]
            ),
            executions=repo,
        )
        result = await runtime.resume(wf, "wrun-1", _ctx(), InProcessEventSink("wrun-1"), _RESUME)

        assert result.status == "failed"
        assert result.error_kind == "strategy"
        assert result.error == "node 'b': boom"

    async def test_no_pause_frame_is_an_internal_terminal(self):
        wf = _workflow([_agent_node("a")])
        repo = RecordingExecutions()
        repo.rows["wrun-1"] = RunResult(
            run_id="wrun-1", agent_id="wf-1", status="awaiting_input", input="hi"
        )
        runtime = _runtime(FakeRunner(), executions=repo)
        result = await runtime.resume(wf, "wrun-1", _ctx(), InProcessEventSink("wrun-1"), _RESUME)

        assert result.status == "failed"
        assert "no pause frame" in (result.error or "")
