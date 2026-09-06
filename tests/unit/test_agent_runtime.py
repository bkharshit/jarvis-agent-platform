"""AgentRuntime orchestrator with all mocks: happy path, tool loop, limits,
timeout, cancel, budget, structured output repair, memory across runs, and
blocking-vs-streamed event-sequence equivalence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    MemoryConfig,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import validate_event_sequence
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.errors import ModelBadRequestError
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.runtime.limits import RunLimits
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.base import BaseTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


class _WaitTool(BaseTool):
    """Blocks until cancelled — used to interrupt a run mid-tool."""

    def __init__(self):
        super().__init__(ToolDescriptor(name="wait_for_cancel", description="waits", parameters={}))

    async def _execute(self, arguments, context):
        await context.cancel.wait()
        context.raise_if_cancelled()
        return "never reached"


class _RecordingRepo:
    """Minimal in-memory doubles for the persistence ports."""

    def __init__(self):
        self.messages: dict[str, list[Message]] = {}
        self.tool_executions: list[tuple[str, object]] = []
        self.started: list[RunResult] = []
        self.finished: list[RunResult] = []
        self.conversations: dict[str, list[Message]] = {}
        self.sequences: dict[str, list[int]] = {}

    # ExecutionRepo subset used by the runtime
    async def create_run(self, result):
        self.started.append(result)

    async def finish_run(self, result):
        self.finished.append(result)

    async def save_message(self, run_id, message):
        self.messages.setdefault(run_id, []).append(message)

    async def save_tool_execution(self, run_id, result, arguments):
        self.tool_executions.append((run_id, result))

    # ConversationRepo
    async def get_or_create(self, agent_id, session_id, *, tenant_id=None):
        return f"{agent_id}:{session_id}"

    async def append_message(self, conversation_id, message, run_id=None):
        seq = len(self.conversations.setdefault(conversation_id, []))
        self.conversations[conversation_id].append(message)
        self.sequences.setdefault(conversation_id, []).append(seq)
        return seq

    async def history(self, conversation_id, limit=None):
        messages = self.conversations.get(conversation_id, [])
        return messages[-limit:] if limit is not None else list(messages)


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


def _runtime(provider: MockModelProvider, tools=(), repo=None, **kw) -> AgentRuntime:
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        strategies=DefaultStrategyRegistry(),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        models=DefaultModelProviderFactory(mock_provider=provider),
        conversations=repo,
        executions=repo,
        **kw,
    )


def _calc_binding():
    from jarvis.tools.builtin.calculator import CalculatorTool

    return CalculatorTool(), ToolBinding(name="calculator")


class TestHappyPath:
    async def test_single_turn_finish(self):
        provider = MockModelProvider([turn("Hello there")])
        runtime = _runtime(provider)
        ctx = _ctx("run-1")

        result = await runtime.run(_version(_agent()), "hi", ctx)

        assert result.status == "succeeded"
        assert result.final_message == "Hello there"
        assert result.iterations == 1
        assert result.error is None
        events = runtime.bus.get("run-1").events
        assert [e.type for e in events] == [
            "run.started",
            "iteration.started",
            "model.invocation.started",
            "text.delta",
            "text.delta",
            "model.invocation.completed",
            "iteration.completed",
            "run.completed",
        ]
        validate_event_sequence(events)

    async def test_tool_loop_then_finish(self):
        calc, binding = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})
                    ]
                ),
                turn("The answer is 42"),
            ]
        )
        agent = _agent(tools=[binding])
        runtime = _runtime(provider, tools=[calc])
        ctx = _ctx("run-2")

        result = await runtime.run(_version(agent), "compute", ctx)

        assert result.status == "succeeded"
        assert result.final_message == "The answer is 42"
        assert result.iterations == 2
        events = runtime.bus.get("run-2").events
        types = [e.type for e in events]
        assert types.count("tool.call.completed") == 1
        completed = next(e for e in events if e.type == "tool.call.completed")
        assert completed.output == "42"
        assert not completed.is_error
        validate_event_sequence(events)

    async def test_tool_failure_emits_failed_event_and_continues(self):
        calc, binding = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": 42})]
                ),
                turn("recovered"),
            ]
        )
        agent = _agent(tools=[binding])
        runtime = _runtime(provider, tools=[calc])

        result = await runtime.run(_version(agent), "x", _ctx("run-3"))

        assert result.status == "succeeded"  # a failed tool call is not a failed run
        events = runtime.bus.get("run-3").events
        failed = [e for e in events if e.type == "tool.call.failed"]
        assert len(failed) == 1
        assert failed[0].kind == "validation"
        validate_event_sequence(events)

    async def test_unknown_tool_binding_is_skipped_not_fatal(self):
        provider = MockModelProvider([turn("ok")])
        agent = _agent(tools=[ToolBinding(name="does_not_exist")])
        runtime = _runtime(provider)

        result = await runtime.run(_version(agent), "x", _ctx("run-4"))
        assert result.status == "succeeded"


class TestLimits:
    async def test_max_iterations_fails(self):
        calc, binding = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id=f"c{i}", name="calculator", arguments={"expression": "1"})
                    ]
                )
                for i in range(4)
            ]
        )
        agent = _agent(tools=[binding], max_iterations=2)
        runtime = _runtime(provider, tools=[calc])

        result = await runtime.run(_version(agent), "loop forever", _ctx("run-5"))

        assert result.status == "failed"
        assert result.error_kind == "max_iterations"
        assert "max_iterations=2" in result.error
        assert result.iterations == 2
        events = runtime.bus.get("run-5").events
        assert events[-1].type == "run.failed"
        validate_event_sequence(events)

    async def test_token_budget_enforced(self):
        calc, binding = _calc_binding()
        provider = MockModelProvider(
            [
                # tool-call turn with heavy usage keeps the loop going to a second
                # iteration, where the accumulated 1100 > 1000 budget trips the check
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "1"})
                    ],
                    usage=Usage(input_tokens=500, output_tokens=600),
                ),
                turn("never invoked"),
            ]
        )
        agent = _agent(tools=[binding])
        runtime = _runtime(
            provider, tools=[calc], limits=RunLimits(max_iterations=32, max_total_tokens=1000)
        )

        result = await runtime.run(_version(agent), "x", _ctx("run-6"))

        assert result.status == "failed"
        assert result.error_kind == "max_iterations"
        assert "token_budget" in result.error
        assert provider.invocations == 1
        events = runtime.bus.get("run-6").events
        validate_event_sequence(events)


class TestTimeoutAndCancel:
    async def test_deadline_exceeded_times_out(self):
        provider = MockModelProvider([turn("never")])
        runtime = _runtime(provider)
        ctx = _ctx("run-7", deadline=datetime.now(UTC) - timedelta(seconds=1))

        result = await runtime.run(_version(_agent()), "x", ctx)

        assert result.status == "timed_out"
        assert result.error_kind == "timeout"
        events = runtime.bus.get("run-7").events
        assert [e.type for e in events] == ["run.started", "run.failed"]
        assert events[-1].error_kind == "timeout"
        validate_event_sequence(events)

    async def test_user_cancel_mid_tool(self):
        provider = MockModelProvider(
            [
                turn(tool_calls=[ToolCall(id="c1", name="wait_for_cancel", arguments={})]),
            ]
        )
        runtime = _runtime(provider, tools=[_WaitTool()])
        ctx = _ctx("run-8")

        task = asyncio.ensure_future(runtime.run(_version(_agent()), "x", ctx))
        await asyncio.sleep(0.05)
        assert runtime.is_live("run-8")
        assert runtime.cancel("run-8", reason="user hit stop")
        result = await task

        assert result.status == "cancelled"
        assert result.error is None
        events = runtime.bus.get("run-8").events
        assert events[-1].type == "run.cancelled"
        assert events[-1].reason == "user hit stop"
        validate_event_sequence(events)
        assert not runtime.is_live("run-8")

    async def test_cancel_unknown_run_returns_false(self):
        runtime = _runtime(MockModelProvider([turn("x")]))
        assert runtime.cancel("nope") is False

    async def test_cancel_is_idempotent(self):
        provider = MockModelProvider(
            [
                turn(tool_calls=[ToolCall(id="c1", name="wait_for_cancel", arguments={})]),
            ]
        )
        runtime = _runtime(provider, tools=[_WaitTool()])
        ctx = _ctx("run-9")

        task = asyncio.ensure_future(runtime.run(_version(_agent()), "x", ctx))
        await asyncio.sleep(0.05)
        assert runtime.cancel("run-9") is True
        assert runtime.cancel("run-9") is True  # second trigger is a no-op
        result = await task
        assert result.status == "cancelled"


class TestModelErrors:
    async def test_model_error_fails_run(self):
        provider = MockModelProvider(
            [
                turn(error=ModelBadRequestError("bad model")),
                turn("never"),
            ]
        )
        runtime = _runtime(provider)

        result = await runtime.run(_version(_agent()), "x", _ctx("run-10"))

        assert result.status == "failed"
        assert result.error_kind == "model"
        events = runtime.bus.get("run-10").events
        assert events[-1].type == "run.failed"
        validate_event_sequence(events)


class TestStructuredOutput:
    def _schema_agent(self):
        return _agent(
            output_schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            }
        )

    async def test_valid_json_passes(self):
        provider = MockModelProvider([turn(json.dumps({"answer": 42}))])
        runtime = _runtime(provider)
        result = await runtime.run(_version(self._schema_agent()), "x", _ctx("run-11"))
        assert result.status == "succeeded"
        assert json.loads(result.final_message) == {"answer": 42}

    async def test_invalid_then_repair_succeeds(self):
        provider = MockModelProvider(
            [
                turn("not json at all"),
                turn(json.dumps({"answer": 7})),
            ]
        )
        runtime = _runtime(provider)
        result = await runtime.run(_version(self._schema_agent()), "x", _ctx("run-12"))
        assert result.status == "succeeded"
        assert result.iterations == 2
        events = runtime.bus.get("run-12").events
        validate_event_sequence(events)

    async def test_repair_exhausted_fails_with_output_schema_kind(self):
        provider = MockModelProvider(
            [
                turn("still not json"),
                turn({"answer": "not an integer"} and json.dumps({"answer": "not an integer"})),
            ]
        )
        runtime = _runtime(provider)
        result = await runtime.run(_version(self._schema_agent()), "x", _ctx("run-13"))
        assert result.status == "failed"
        assert result.error_kind == "output_schema"
        assert provider.invocations == 2  # exactly ONE repair retry
        events = runtime.bus.get("run-13").events
        assert events[-1].error_kind == "output_schema"
        validate_event_sequence(events)


class TestPersistenceHooks:
    async def test_messages_and_tool_rows_and_finish_recorded(self):
        calc, binding = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "1+1"})
                    ]
                ),
                turn("done"),
            ]
        )
        agent = _agent(tools=[binding])
        repo = _RecordingRepo()
        runtime = _runtime(provider, tools=[calc], repo=repo)

        result = await runtime.run(_version(agent), "compute", _ctx("run-14"))

        assert repo.finished == [result]
        roles = [m.role for m in repo.messages["run-14"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert len(repo.tool_executions) == 1
        _run_id, tool_result = repo.tool_executions[0]
        assert tool_result.output == "2"


class TestMemoryAcrossRuns:
    async def test_history_window_passed_between_runs(self):
        repo = _RecordingRepo()
        agent = _agent(memory=MemoryConfig(enabled=True, max_messages=4))

        # run 1: the model sees only the fresh input (empty history)
        provider1 = MockModelProvider([turn("first")])
        runtime1 = _runtime(provider1, repo=repo)
        ctx1 = _ctx("run-15", session_id="sess-1")
        await runtime1.run(_version(agent), "what is my name?", ctx1)

        # run 2, same session: history from run 1 is in the prompt
        provider2 = MockModelProvider([turn("second")])
        runtime2 = _runtime(provider2, repo=repo)
        ctx2 = _ctx("run-16", session_id="sess-1")
        await runtime2.run(_version(agent), "and now?", ctx2)

        request2 = provider2.requests[0]
        prompt = [m for m in request2.messages if m.role == "user"]
        assert any("what is my name?" in m.text for m in prompt)
        # conversation persists across runtimes (repo shared)
        conversation = repo.conversations["a1:sess-1"]
        assert [m.content for m in conversation] == [
            "what is my name?",
            "first",
            "and now?",
            "second",
        ]

    async def test_no_session_means_no_memory(self):
        repo = _RecordingRepo()
        agent = _agent(memory=MemoryConfig(enabled=True))
        provider = MockModelProvider([turn("solo")])
        runtime = _runtime(provider, repo=repo)
        await runtime.run(_version(agent), "x", _ctx("run-17"))
        assert repo.conversations == {}
        assert provider.requests[0].messages[-1].text == "x"


class TestBlockingVsStreamedEquivalence:
    async def test_identical_event_sequences(self):
        calc, binding = _calc_binding()

        def script():
            return [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"})
                    ]
                ),
                turn("The result is 4"),
            ]

        agent = _agent(tools=[binding])

        # blocking mode: caller owns the sink, reads events after run() returns
        blocking_provider = MockModelProvider(script())
        blocking_runtime = _runtime(blocking_provider, tools=[calc])
        blocking_sink = await blocking_runtime.bus.get_or_create("run-block")
        await blocking_runtime.run(
            _version(agent), "compute", _ctx("run-block"), sink=blocking_sink
        )
        blocking_types = [e.type for e in blocking_sink.events]

        # streamed mode: a subscriber consumes the sink live while the run executes
        streaming_provider = MockModelProvider(script())
        streaming_runtime = _runtime(streaming_provider, tools=[calc])
        streaming_sink = await streaming_runtime.bus.get_or_create("run-stream")
        received: list[str] = []

        async def _subscribe():
            async for _cursor, event in streaming_sink.subscribe():
                received.append(event.type)
                if event.type in ("run.completed", "run.failed", "run.cancelled"):
                    break

        subscriber = asyncio.ensure_future(_subscribe())
        await asyncio.sleep(0.01)
        await streaming_runtime.run(
            _version(agent), "compute", _ctx("run-stream"), sink=streaming_sink
        )
        await subscriber

        assert received == blocking_types
        validate_event_sequence(streaming_sink.events)
