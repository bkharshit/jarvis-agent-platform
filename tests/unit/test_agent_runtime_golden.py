"""Golden event-sequence tests (S6 commit 4): the `_run_segment` extraction
from `AgentRuntime.run()` must be event-for-event invisible.

Each scenario asserts the run's FULL event log as canonical JSON dicts —
volatile fields (event_id, created_at, sequence, latency_ms, awaiting_until,
agent_version_id) stripped — captured against the PRE-refactor code. The
literals below ARE the gate: after the refactor they must still pass
byte-identically. If a sequence diverges, fix before proceeding — nothing
downstream (the workflow runtime riding `_run_segment`) may sit on an
unverified extraction."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ModelRef,
    StrategyConfig,
    ToolBinding,
)
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall
from jarvis.events.bus import InProcessEventSink
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.ports.queue import ResumeRequest
from jarvis.ports.strategy import AskHumanStep, FinishStep
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime

VOLATILE = {
    "event_id",
    "created_at",
    "sequence",
    "latency_ms",
    "awaiting_until",
    "agent_version_id",
}


def _canonical(events) -> list[dict]:
    out = []
    for event in events:
        data = event.model_dump(mode="json")
        for key in VOLATILE:
            data.pop(key, None)
        out.append(data)
    return out


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


def _runtime(provider: MockModelProvider, tools=(), strategies=None) -> AgentRuntime:
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        strategies=strategies or DefaultStrategyRegistry(),
        tools=registry,
        tool_runtime=ToolRuntime(registry),
        models=DefaultModelProviderFactory(mock_provider=provider),
    )


_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class TestGoldenFreshRun:
    async def test_single_turn_sequence_is_byte_identical(self):
        runtime = _runtime(MockModelProvider([turn("Hello there")]))

        result = await runtime.run(_version(_agent()), "hi", _ctx("g-fresh"))

        assert result.status == "succeeded"
        assert _canonical(runtime.bus.get("g-fresh").events) == [
            {
                "run_id": "g-fresh",
                "type": "run.started",
                "agent_id": "a1",
                "session_id": None,
                "input": "hi",
            },
            {"run_id": "g-fresh", "type": "iteration.started", "iteration": 0},
            {"run_id": "g-fresh", "type": "model.invocation.started", "attempt": 1},
            {"run_id": "g-fresh", "type": "text.delta", "text": "Hello"},
            {"run_id": "g-fresh", "type": "text.delta", "text": " there"},
            {
                "run_id": "g-fresh",
                "type": "model.invocation.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "finish_reason": "stop",
                "model": "mock-1",
            },
            {
                "run_id": "g-fresh",
                "type": "iteration.completed",
                "iteration": 0,
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
            },
            {
                "run_id": "g-fresh",
                "type": "run.completed",
                "final_message": "Hello there",
                "total_usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "iterations": 1,
            },
        ]


class TestGoldenToolLoop:
    async def test_tool_round_trip_sequence_is_byte_identical(self):
        calc = CalculatorTool()
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
        runtime = _runtime(provider, tools=[calc])

        result = await runtime.run(
            _version(_agent(tools=[ToolBinding(name="calculator")])), "compute", _ctx("g-tools")
        )

        assert result.status == "succeeded"
        assert _canonical(runtime.bus.get("g-tools").events) == [
            {
                "run_id": "g-tools",
                "type": "run.started",
                "agent_id": "a1",
                "session_id": None,
                "input": "compute",
            },
            {"run_id": "g-tools", "type": "iteration.started", "iteration": 0},
            {"run_id": "g-tools", "type": "model.invocation.started", "attempt": 1},
            {
                "run_id": "g-tools",
                "type": "model.invocation.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "finish_reason": "stop",
                "model": "mock-1",
            },
            {
                "run_id": "g-tools",
                "type": "tool.call.requested",
                "tool_call_id": "c1",
                "name": "calculator",
                "arguments": {"expression": "6*7"},
            },
            {
                "run_id": "g-tools",
                "type": "tool.call.started",
                "tool_call_id": "c1",
                "name": "calculator",
            },
            {
                "run_id": "g-tools",
                "type": "tool.call.completed",
                "tool_call_id": "c1",
                "name": "calculator",
                "output": "42",
                "is_error": False,
            },
            {
                "run_id": "g-tools",
                "type": "iteration.completed",
                "iteration": 0,
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
            },
            {"run_id": "g-tools", "type": "iteration.started", "iteration": 1},
            {"run_id": "g-tools", "type": "model.invocation.started", "attempt": 1},
            {"run_id": "g-tools", "type": "text.delta", "text": "The"},
            {"run_id": "g-tools", "type": "text.delta", "text": " answer"},
            {"run_id": "g-tools", "type": "text.delta", "text": " is"},
            {"run_id": "g-tools", "type": "text.delta", "text": " 42"},
            {
                "run_id": "g-tools",
                "type": "model.invocation.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "finish_reason": "stop",
                "model": "mock-1",
            },
            {
                "run_id": "g-tools",
                "type": "iteration.completed",
                "iteration": 1,
                "usage": {"input_tokens": 20, "output_tokens": 10, "extra": {}},
            },
            {
                "run_id": "g-tools",
                "type": "run.completed",
                "final_message": "The answer is 42",
                "total_usage": {"input_tokens": 20, "output_tokens": 10, "extra": {}},
                "iterations": 2,
            },
        ]


class TestGoldenStructuredRepair:
    async def test_repair_sequence_is_byte_identical(self):
        provider = MockModelProvider([turn("not json at all"), turn(json.dumps({"answer": 7}))])
        runtime = _runtime(provider)

        result = await runtime.run(_version(_agent(output_schema=_SCHEMA)), "x", _ctx("g-repair"))

        assert result.status == "succeeded"
        assert _canonical(runtime.bus.get("g-repair").events) == [
            {
                "run_id": "g-repair",
                "type": "run.started",
                "agent_id": "a1",
                "session_id": None,
                "input": "x",
            },
            {"run_id": "g-repair", "type": "iteration.started", "iteration": 0},
            {"run_id": "g-repair", "type": "model.invocation.started", "attempt": 1},
            {"run_id": "g-repair", "type": "text.delta", "text": "not"},
            {"run_id": "g-repair", "type": "text.delta", "text": " json"},
            {"run_id": "g-repair", "type": "text.delta", "text": " at"},
            {"run_id": "g-repair", "type": "text.delta", "text": " all"},
            {
                "run_id": "g-repair",
                "type": "model.invocation.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "finish_reason": "stop",
                "model": "mock-1",
            },
            {
                "run_id": "g-repair",
                "type": "iteration.completed",
                "iteration": 0,
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
            },
            {"run_id": "g-repair", "type": "iteration.started", "iteration": 1},
            {"run_id": "g-repair", "type": "model.invocation.started", "attempt": 1},
            {"run_id": "g-repair", "type": "text.delta", "text": '{"answer":'},
            {"run_id": "g-repair", "type": "text.delta", "text": " 7}"},
            {
                "run_id": "g-repair",
                "type": "model.invocation.completed",
                "usage": {"input_tokens": 10, "output_tokens": 5, "extra": {}},
                "finish_reason": "stop",
                "model": "mock-1",
            },
            {
                "run_id": "g-repair",
                "type": "iteration.completed",
                "iteration": 1,
                "usage": {"input_tokens": 20, "output_tokens": 10, "extra": {}},
            },
            {
                "run_id": "g-repair",
                "type": "run.completed",
                "final_message": '{"answer": 7}',
                "total_usage": {"input_tokens": 20, "output_tokens": 10, "extra": {}},
                "iterations": 2,
            },
        ]


class TestGoldenPauseAndResume:
    async def test_pause_and_resume_segments_are_byte_identical(self):
        class _AskHuman:
            name = "ask_human_fixture"

            def __init__(self) -> None:
                self.calls = 0

            async def step(self, ctx, messages, client, tools, sink):
                self.calls += 1
                if self.calls > 1:
                    return FinishStep(
                        assistant_message=Message(
                            role="assistant", content=f"deploying to {messages[-1].text}"
                        )
                    )
                return AskHumanStep(
                    assistant_message=Message(role="assistant", content="Which environment?"),
                    question="Which environment?",
                )

        strategy = _AskHuman()
        runtime = _runtime(
            MockModelProvider([turn("never")]),
            strategies=SimpleNamespace(resolve=lambda config: strategy),
        )

        first = await runtime.run(_version(_agent()), "deploy", _ctx("g-pause"))
        assert first.status == "awaiting_input"
        first_events = _canonical(runtime.bus.get("g-pause").events)

        sink = InProcessEventSink("g-pause", sequence_offset=len(runtime.bus.get("g-pause").events))
        second = await runtime.resume(
            _version(_agent()),
            "g-pause",
            _ctx("g-pause"),
            sink,
            ResumeRequest(kind="content", content="prod"),
        )
        assert second.status == "succeeded"
        second_events = _canonical(sink.events)

        assert first_events == [
            {
                "run_id": "g-pause",
                "type": "run.started",
                "agent_id": "a1",
                "session_id": None,
                "input": "deploy",
            },
            {"run_id": "g-pause", "type": "iteration.started", "iteration": 0},
            {
                "run_id": "g-pause",
                "type": "run.awaiting_input",
                "reason": "strategy",
                "question": "Which environment?",
                "pending_calls": [],
            },
        ]
        assert second_events == [
            {
                "run_id": "g-pause",
                "type": "iteration.completed",
                "iteration": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "extra": {}},
            },
            {
                "run_id": "g-pause",
                "type": "run.completed",
                "final_message": "deploying to prod",
                "total_usage": {"input_tokens": 0, "output_tokens": 0, "extra": {}},
                "iterations": 1,
            },
        ]


class TestGoldenTerminalFailures:
    """The failure terminals ride the same try — their sequences are golden
    too (error text naming the failure, kind, event shapes)."""

    async def test_model_error_sequence_is_byte_identical(self):
        from jarvis.models.errors import ModelBadRequestError

        class _FailingProvider:
            async def resolve(self, ref, *, principal=None):
                raise ModelBadRequestError("boom", provider="mock", model="mock-1")

        registry = InMemoryToolRegistry()
        runtime = AgentRuntime(
            strategies=DefaultStrategyRegistry(),
            tools=registry,
            tool_runtime=ToolRuntime(registry),
            models=SimpleNamespace(resolve=_FailingProvider().resolve),
        )

        result = await runtime.run(_version(_agent()), "hi", _ctx("g-mfail"))

        assert result.status == "failed"
        assert result.error_kind == "model"
        assert _canonical(runtime.bus.get("g-mfail").events) == [
            {
                "run_id": "g-mfail",
                "type": "run.failed",
                "error": "boom",
                "error_kind": "model",
                "total_usage": {"input_tokens": 0, "output_tokens": 0, "extra": {}},
            },
        ]
