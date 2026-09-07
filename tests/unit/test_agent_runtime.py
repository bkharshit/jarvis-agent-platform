"""AgentRuntime orchestrator with all mocks: happy path, tool loop, limits,
timeout, cancel, budget, structured output repair, memory across runs, and
blocking-vs-streamed event-sequence equivalence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    MemoryConfig,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.events import EventSequenceError, TextDelta, validate_event_sequence
from jarvis.domain.execution import ExecutionContext, RunResult
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolDescriptor
from jarvis.events.bus import InProcessEventSink
from jarvis.models.errors import ModelBadRequestError
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.ports.queue import ResumeRequest
from jarvis.ports.strategy import AskHumanStep, FinishStep
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
        self.awaited: list[tuple[str, object]] = []
        self.runs: dict[str, RunResult] = {}
        self.events: dict[str, list[object]] = {}
        self.conversations: dict[str, list[Message]] = {}
        self.sequences: dict[str, list[int]] = {}

    # ExecutionRepo subset used by the runtime
    async def create_run(self, result):
        self.started.append(result)
        self.runs[result.run_id] = result

    async def finish_run(self, result):
        self.finished.append(result)
        self.runs[result.run_id] = result

    async def mark_awaiting_input(self, run_id, awaiting_until, *, total_usage=None):
        self.awaited.append((run_id, awaiting_until))
        if run_id in self.runs:
            self.runs[run_id] = self.runs[run_id].model_copy(
                update={"status": "awaiting_input", "total_usage": total_usage}
            )

    async def get(self, run_id):
        return self.runs.get(run_id)

    async def list_messages(self, run_id):
        return list(self.messages.get(run_id, []))

    def record_event(self, event):
        """The bus persists through the sink; unit sinks are unpersisted, so
        resume tests seed list_events manually from the first segment."""
        self.events.setdefault(event.run_id, []).append(event)

    async def list_events(self, run_id):
        for event in self.events.get(run_id, []):
            yield event

    async def save_message(self, run_id, message):
        self.messages.setdefault(run_id, []).append(message)

    async def save_tool_execution(self, run_id, result, arguments):
        self.tool_executions.append((run_id, result))

    # ConversationRepo
    async def get_or_create(self, agent_id, session_id, *, tenant_id=None):
        return f"{agent_id}:{session_id}"

    async def find(self, agent_id, session_id):
        key = f"{agent_id}:{session_id}"
        return key if key in self.conversations else None

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


_APPROVAL_CALL = [ToolCall(id="c1", name="calculator", arguments={"expression": "1"})]


class TestResolutionFailure:
    """Model resolution happens inside the runtime's try (S2: credential
    resolution is IO and can fail) — the failure is a persisted terminal
    `model` state, never an exception past the runtime (D5). An escape would
    make the worker treat the message as a claim failure and retry forever."""

    async def test_resolution_failure_is_terminal_model_failure(self):
        from jarvis.models.errors import ModelAuthError

        class _FailingFactory:
            async def resolve(self, ref, *, principal=None):
                raise ModelAuthError(
                    "credential 'c1' not found", provider="openai_compatible", model="m"
                )

        registry = InMemoryToolRegistry()
        runtime = AgentRuntime(
            strategies=DefaultStrategyRegistry(),
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            models=SimpleNamespace(resolve=_FailingFactory().resolve),
            conversations=None,
            executions=None,
        )
        ctx = _ctx("run-fail")

        result = await runtime.run(_version(_agent()), "hi", ctx)

        assert result.status == "failed"
        assert result.error_kind == "model"
        assert "not found" in (result.error or "")

    async def test_resolution_failure_never_raises(self):
        from jarvis.models.errors import ModelAuthError

        async def _boom(ref, *, principal=None):
            raise ModelAuthError("boom", provider="p", model="m")

        registry = InMemoryToolRegistry()
        runtime = AgentRuntime(
            strategies=DefaultStrategyRegistry(),
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            models=SimpleNamespace(resolve=_boom),
            conversations=None,
            executions=None,
        )
        result = await runtime.run(_version(_agent()), "hi", _ctx("run-fail-2"))
        assert result.status == "failed"


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


class _AskHumanStrategy:
    """Fixture strategy: asks the human, then finishes with the answer."""

    name = "ask_human_fixture"

    def __init__(self, question: str = "Which environment?", asks: int = 1):
        self._question = question
        self._asks = asks
        self.answered_with: str | None = None
        self.calls = 0

    async def step(self, ctx, messages, client, tools, sink):
        self.calls += 1
        # Asks `asks` times, then finishes with the newest user message (the
        # human's resume answer).
        last = messages[-1]
        if self.calls > self._asks:
            self.answered_with = last.text
            return FinishStep(
                assistant_message=Message(role="assistant", content=f"deploying to {last.text}")
            )
        return AskHumanStep(
            assistant_message=Message(role="assistant", content=self._question),
            question=self._question,
        )


class TestPauseToolApproval:
    def _gated_binding(self):
        return ToolBinding(name="calculator", config={"requires_approval": True})

    async def test_gated_tool_pauses_with_pending_calls(self):
        calc, _ = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})
                    ]
                ),
                turn("never reached in this segment"),
            ]
        )
        agent = _agent(tools=[self._gated_binding()])
        repo = _RecordingRepo()
        runtime = _runtime(provider, tools=[calc], repo=repo)

        result = await runtime.run(_version(agent), "compute", _ctx("run-pause-1"))

        assert result.status == "awaiting_input"
        assert result.finished_at is None  # paused, not finished
        assert repo.finished == []  # no terminal write
        assert len(repo.awaited) == 1  # mark_awaiting_input(run_id, until)
        events = runtime.bus.get("run-pause-1").events
        types = [e.type for e in events]
        assert types[-1] == "run.awaiting_input"
        assert types.count("tool.call.requested") == 1
        assert "tool.call.started" not in types  # nothing executed yet
        pause = events[-1]
        assert pause.reason == "tool_approval"
        assert [c.id for c in pause.pending_calls] == ["c1"]
        assert pause.awaiting_until > datetime.now(UTC)
        # Gapless within the segment; the pause ends it (no terminal yet).
        assert [e.sequence for e in events] == list(range(len(events)))
        # The segment is closed — further appends on this sink are refused.
        with pytest.raises(EventSequenceError, match="segment at a pause"):
            sink = runtime.bus.get("run-pause-1")
            await sink.append(TextDelta(event_id="x", run_id="run-pause-1", text="late"))

    async def test_annotation_gates_without_binding_config(self):
        class _GatedTimeTool(BaseTool):
            def __init__(self):
                super().__init__(
                    ToolDescriptor(
                        name="current_time",
                        description="time",
                        parameters={},
                        annotations={"requires_approval": True},
                    )
                )

            async def _execute(self, arguments, context):
                return "12:00"

        provider = MockModelProvider(
            [turn(tool_calls=[ToolCall(id="c1", name="current_time", arguments={})]), turn("x")]
        )
        agent = _agent(tools=[ToolBinding(name="current_time")])
        runtime = _runtime(provider, tools=[_GatedTimeTool()])

        result = await runtime.run(_version(agent), "time?", _ctx("run-pause-2"))

        assert result.status == "awaiting_input"
        pending = runtime.bus.get("run-pause-2").events[-1].pending_calls
        assert [c.name for c in pending] == ["current_time"]

    async def test_binding_config_wins_over_descriptor(self):
        calc, _ = _calc_binding()
        calc.descriptor.annotations = {"requires_approval": True}

        # Binding says NOT required — the descriptor's gate is overridden.
        provider = MockModelProvider(
            [
                turn(tool_calls=_APPROVAL_CALL),
                turn("ok"),
            ]
        )
        agent = _agent(tools=[ToolBinding(name="calculator", config={"requires_approval": False})])
        runtime = _runtime(provider, tools=[calc])
        result = await runtime.run(_version(agent), "x", _ctx("run-pause-3a"))
        assert result.status == "succeeded"

        # Binding says required — wins the same way.
        provider2 = MockModelProvider(
            [
                turn(tool_calls=_APPROVAL_CALL),
                turn("x"),
            ]
        )
        agent2 = _agent(tools=[ToolBinding(name="calculator", config={"requires_approval": True})])
        runtime2 = _runtime(provider2, tools=[calc])
        result2 = await runtime2.run(_version(agent2), "x", _ctx("run-pause-3b"))
        assert result2.status == "awaiting_input"

    async def test_only_gated_calls_are_pending(self):
        from jarvis.tools.builtin.current_time import CurrentTimeTool

        calc, _ = _calc_binding()
        now_tool = CurrentTimeTool()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "1"}),
                        ToolCall(id="c2", name="current_time", arguments={}),
                    ]
                ),
                turn("x"),
            ]
        )
        agent = _agent(tools=[self._gated_binding(), ToolBinding(name="current_time")])
        runtime = _runtime(provider, tools=[calc, now_tool])

        result = await runtime.run(_version(agent), "x", _ctx("run-pause-4"))

        assert result.status == "awaiting_input"
        pending = runtime.bus.get("run-pause-4").events[-1].pending_calls
        assert [c.id for c in pending] == ["c1"]  # the ungated call is not pending
        events = runtime.bus.get("run-pause-4").events
        started = [e for e in events if e.type == "tool.call.started"]
        assert started == []  # nothing executed while gated calls are pending


class TestPauseStrategyAsk:
    async def test_ask_human_step_pauses_with_question(self):
        repo = _RecordingRepo()
        strategy = _AskHumanStrategy("Which environment?")
        runtime = AgentRuntime(
            strategies=SimpleNamespace(resolve=lambda config: strategy),
            tools=InMemoryToolRegistry(),
            tool_runtime=ToolRuntime(InMemoryToolRegistry()),
            models=DefaultModelProviderFactory(mock_provider=MockModelProvider([turn("never")])),
            conversations=repo,
            executions=repo,
        )

        result = await runtime.run(_version(_agent()), "deploy", _ctx("run-pause-5"))

        assert result.status == "awaiting_input"
        assert repo.finished == []
        assert len(repo.awaited) == 1
        # The assistant's question is persisted as a message.
        roles = [m.role for m in repo.messages["run-pause-5"]]
        assert roles == ["user", "assistant"]
        events = runtime.bus.get("run-pause-5").events
        assert [e.type for e in events] == [
            "run.started",
            "iteration.started",
            "run.awaiting_input",
        ]
        pause = events[-1]
        assert pause.reason == "strategy"
        assert pause.question == "Which environment?"
        assert pause.pending_calls == []

    async def test_ask_human_strategy_without_repo_still_pauses(self):
        strategy = _AskHumanStrategy()
        runtime = AgentRuntime(
            strategies=SimpleNamespace(resolve=lambda config: strategy),
            tools=InMemoryToolRegistry(),
            tool_runtime=ToolRuntime(InMemoryToolRegistry()),
            models=DefaultModelProviderFactory(mock_provider=MockModelProvider([turn("never")])),
            conversations=None,
            executions=None,
        )
        result = await runtime.run(_version(_agent()), "deploy", _ctx("run-pause-6"))
        assert result.status == "awaiting_input"


# --- resume segments (S10, ADR 0010 §4) --------------------------------------


def _seed_events(repo: _RecordingRepo, sink) -> None:
    """Copy the first segment's events into the repo's durable log (the unit
    bus has no persist callback) so the resume path can replay them."""
    for event in sink.events:
        repo.record_event(event)


def _resume_sink(repo: _RecordingRepo, run_id: str) -> InProcessEventSink:
    """A sink for the resumed segment, seeded at the durable log's next
    sequence — the same seeding the worker does via next_event_sequence."""
    return InProcessEventSink(run_id, sequence_offset=len(repo.events.get(run_id, [])))


class TestResumeStrategyAsk:
    def _runtime(self, strategy, provider, repo):
        return AgentRuntime(
            strategies=SimpleNamespace(resolve=lambda config: strategy),
            tools=InMemoryToolRegistry(),
            tool_runtime=ToolRuntime(InMemoryToolRegistry()),
            models=DefaultModelProviderFactory(mock_provider=provider),
            conversations=repo,
            executions=repo,
        )

    async def test_content_resume_completes_and_strategy_sees_answer(self):
        repo = _RecordingRepo()
        strategy = _AskHumanStrategy("Which environment?")
        runtime = self._runtime(strategy, MockModelProvider([turn("never")]), repo)
        first = await runtime.run(_version(_agent()), "deploy", _ctx("run-hl-1"))
        assert first.status == "awaiting_input"
        assert repo.runs["run-hl-1"].status == "awaiting_input"
        _seed_events(repo, runtime.bus.get("run-hl-1"))

        sink = _resume_sink(repo, "run-hl-1")
        result = await runtime.resume(
            _version(_agent()),
            "run-hl-1",
            _ctx("run-hl-1"),
            sink,
            ResumeRequest(kind="content", content="prod"),
        )

        assert result.status == "succeeded"
        assert result.final_message == "deploying to prod"
        assert strategy.answered_with == "prod"
        # one terminal write, and the row finished
        assert [r.status for r in repo.finished] == ["succeeded"]
        assert repo.runs["run-hl-1"].status == "succeeded"
        # the answer was persisted as the newest message
        roles = [m.role for m in repo.messages["run-hl-1"]]
        assert roles == ["user", "assistant", "user", "assistant"]
        # the segment continues, it does not restart: no run.started, the
        # paused iteration closes, then the terminal
        types = [e.type for e in sink.events]
        assert "run.started" not in types
        assert types == ["iteration.completed", "run.completed"]
        # gapless ACROSS segments: the resumed sequence continues the first's
        first_events = runtime.bus.get("run-hl-1").events
        first_seqs = [e.sequence for e in first_events]
        second_seqs = [e.sequence for e in sink.events]
        assert second_seqs == list(range(len(first_seqs), len(first_seqs) + len(second_seqs)))
        validate_event_sequence(first_events + sink.events)

    async def test_resume_can_pause_again(self):
        repo = _RecordingRepo()
        strategy = _AskHumanStrategy(asks=2)
        runtime = self._runtime(strategy, MockModelProvider([turn("never")]), repo)
        agent = _agent(memory=MemoryConfig(enabled=True))
        ctx = _ctx("run-hl-2", session_id="s1")
        await runtime.run(_version(agent), "deploy", ctx)
        assert repo.runs["run-hl-2"].status == "awaiting_input"
        _seed_events(repo, runtime.bus.get("run-hl-2"))

        sink1 = _resume_sink(repo, "run-hl-2")
        paused_again = await runtime.resume(
            _version(agent),
            "run-hl-2",
            _ctx("run-hl-2", session_id="s1"),
            sink1,
            ResumeRequest(kind="content", content="still not enough"),
        )
        assert paused_again.status == "awaiting_input"
        assert repo.runs["run-hl-2"].status == "awaiting_input"
        assert [r.status for r in repo.finished] == []  # still no terminal
        # the answer reached the conversation transcript for the next segment
        assert any(m.content == "still not enough" for m in repo.conversations["a1:s1"])
        _seed_events(repo, sink1)

        sink2 = _resume_sink(repo, "run-hl-2")
        done = await runtime.resume(
            _version(agent),
            "run-hl-2",
            _ctx("run-hl-2", session_id="s1"),
            sink2,
            ResumeRequest(kind="content", content="prod"),
        )
        assert done.status == "succeeded"
        assert strategy.answered_with == "prod"

    async def test_mismatched_approval_resume_degrades_to_answer_path(self):
        repo = _RecordingRepo()
        strategy = _AskHumanStrategy()
        runtime = self._runtime(strategy, MockModelProvider([turn("never")]), repo)
        await runtime.run(_version(_agent()), "deploy", _ctx("run-hl-3"))
        _seed_events(repo, runtime.bus.get("run-hl-3"))

        sink = _resume_sink(repo, "run-hl-3")
        result = await runtime.resume(
            _version(_agent()),
            "run-hl-3",
            _ctx("run-hl-3"),
            sink,
            ResumeRequest(kind="tool_approval", approved=True),
        )
        # no batch existed to execute — the loop re-invoked the strategy
        assert result.status == "succeeded"
        types = [e.type for e in sink.events]
        assert "tool.call.started" not in types


class TestResumeToolApproval:
    def _gated_binding(self):
        return ToolBinding(name="calculator", config={"requires_approval": True})

    async def _paused(self, provider, tools, agent, run_id) -> tuple[AgentRuntime, _RecordingRepo]:
        repo = _RecordingRepo()
        runtime = _runtime(provider, tools=tools, repo=repo)
        result = await runtime.run(_version(agent), "x", _ctx(run_id))
        assert result.status == "awaiting_input"
        _seed_events(repo, runtime.bus.get(run_id))
        return runtime, repo

    async def test_approved_batch_executes_and_run_completes(self):
        calc, _ = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"})
                    ]
                ),
                turn("42 it is"),
            ]
        )
        agent = _agent(tools=[self._gated_binding()])
        runtime, repo = await self._paused(provider, [calc], agent, "run-hl-4")

        sink = _resume_sink(repo, "run-hl-4")
        result = await runtime.resume(
            _version(agent),
            "run-hl-4",
            _ctx("run-hl-4"),
            sink,
            ResumeRequest(kind="tool_approval", approved=True),
        )

        assert result.status == "succeeded"
        assert result.final_message == "42 it is"
        types = [e.type for e in sink.events]
        # the paused iteration continued: the batch executes, then it closes
        assert types[:3] == ["tool.call.started", "tool.call.completed", "iteration.completed"]
        assert "tool.call.requested" not in types  # already emitted pre-pause
        assert types.count("tool.call.started") == 1
        assert types[-1] == "run.completed"
        # the executed tool result landed in the transcript
        tool_messages = [m for m in repo.messages["run-hl-4"] if m.role == "tool"]
        assert tool_messages and "42" in tool_messages[-1].content
        # sequences continue across the segment boundary
        first = runtime.bus.get("run-hl-4").events
        assert [e.sequence for e in sink.events] == list(
            range(len(first), len(first) + len(sink.events))
        )
        validate_event_sequence(first + sink.events)

    async def test_refusal_refuses_gated_and_executes_ungated(self):
        from jarvis.tools.builtin.current_time import CurrentTimeTool

        calc, _ = _calc_binding()
        now_tool = CurrentTimeTool()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "1"}),
                        ToolCall(id="c2", name="current_time", arguments={}),
                    ]
                ),
                turn("done"),
            ]
        )
        agent = _agent(tools=[self._gated_binding(), ToolBinding(name="current_time")])
        runtime, repo = await self._paused(provider, [calc, now_tool], agent, "run-hl-5")

        sink = _resume_sink(repo, "run-hl-5")
        result = await runtime.resume(
            _version(agent),
            "run-hl-5",
            _ctx("run-hl-5"),
            sink,
            ResumeRequest(kind="tool_approval", approved=False),
        )

        assert result.status == "succeeded"
        # only the ungated call executed
        started = [e for e in sink.events if e.type == "tool.call.started"]
        assert [e.tool_call_id for e in started] == ["c2"]
        # the decision is an event (ADR 0011 §3) — the refusal, not an execution
        declined = [e for e in sink.events if e.type == "tool.call.declined"]
        assert [(e.tool_call_id, e.name) for e in declined] == [("c1", "calculator")]
        # the refused call closed with a refusal tool message
        refused = [m for m in repo.messages["run-hl-5"] if m.tool_call_id == "c1"]
        assert len(refused) == 1
        assert "declined" in refused[0].content

    async def _paused_two_gated(
        self, run_id
    ) -> tuple[AgentRuntime, _RecordingRepo, AgentDefinition]:
        calc, _ = _calc_binding()
        provider = MockModelProvider(
            [
                turn(
                    tool_calls=[
                        ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"}),
                        ToolCall(id="c2", name="calculator", arguments={"expression": "2+2"}),
                    ]
                ),
                turn("done"),
            ]
        )
        agent = _agent(tools=[self._gated_binding()])
        runtime, repo = await self._paused(provider, [calc], agent, run_id)
        return runtime, repo, agent

    async def test_decisions_approve_one_refuse_one(self):
        # ADR 0011: per-call verdicts — c1 executes, c2 is refused.
        runtime, repo, agent = await self._paused_two_gated("run-hl-6")

        sink = _resume_sink(repo, "run-hl-6")
        result = await runtime.resume(
            _version(agent),
            "run-hl-6",
            _ctx("run-hl-6"),
            sink,
            ResumeRequest(kind="decisions", decisions={"c1": True, "c2": False}),
        )

        assert result.status == "succeeded"
        started = [e for e in sink.events if e.type == "tool.call.started"]
        assert [e.tool_call_id for e in started] == ["c1"]
        declined = [e for e in sink.events if e.type == "tool.call.declined"]
        assert [e.tool_call_id for e in declined] == ["c2"]
        refused = [m for m in repo.messages["run-hl-6"] if m.tool_call_id == "c2"]
        assert len(refused) == 1
        assert "declined" in refused[0].content
        executed = [m for m in repo.messages["run-hl-6"] if m.tool_call_id == "c1"]
        assert executed and "42" in executed[-1].content
        validate_event_sequence(runtime.bus.get("run-hl-6").events + sink.events)

    async def test_decisions_default_denies_unmentioned_calls(self):
        # A call absent from the map is declined — silence never approves.
        runtime, repo, agent = await self._paused_two_gated("run-hl-7")

        sink = _resume_sink(repo, "run-hl-7")
        result = await runtime.resume(
            _version(agent),
            "run-hl-7",
            _ctx("run-hl-7"),
            sink,
            ResumeRequest(kind="decisions", decisions={"c1": True}),
        )

        assert result.status == "succeeded"
        started = [e for e in sink.events if e.type == "tool.call.started"]
        assert [e.tool_call_id for e in started] == ["c1"]
        declined = [e for e in sink.events if e.type == "tool.call.declined"]
        assert [e.tool_call_id for e in declined] == ["c2"]
        refused = [m for m in repo.messages["run-hl-7"] if m.tool_call_id == "c2"]
        assert len(refused) == 1
        assert "declined" in refused[0].content
