"""Strategies: FunctionCalling (incl. rate-limit retry, response_format,
streaming accumulation) and ReAct (parsing, recovery, retry)."""

import pytest

from jarvis.domain.agent import ModelRef, StrategyConfig
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.events.bus import InProcessEventSink
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.errors import ModelBadRequestError, ModelRateLimitError
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.models.mock import MockModelProvider, turn
from jarvis.strategies.function_calling import FunctionCallingStrategy
from jarvis.strategies.react import ReActStrategy
from jarvis.strategies.registry import DefaultStrategyRegistry, UnknownStrategyError

CALC = ToolDescriptor(name="calculator", description="math", parameters={"type": "object"})
TOOLS = [CALC]


def _ctx(run_id="r1", **kw) -> ExecutionContext:
    return ExecutionContext(run_id=run_id, agent_id="a1", agent_version_id="v1", **kw)


async def _client(provider) -> object:
    return await DefaultModelProviderFactory(mock_provider=provider).resolve(
        ModelRef(provider="mock", model="mock-1")
    )


class TestFunctionCalling:
    async def test_tool_calls_step(self):
        provider = MockModelProvider(
            [turn(tool_calls=[ToolCall(id="c1", name="calculator", arguments={"e": 1})])]
        )
        strategy = FunctionCallingStrategy()
        sink = InProcessEventSink("r1")
        step = await strategy.step(
            _ctx(), [Message(role="user", content="x")], await _client(provider), TOOLS, sink
        )
        assert step.kind == "tool_calls"
        assert step.tool_calls[0].name == "calculator"

    async def test_finish_step(self):
        provider = MockModelProvider([turn("All done")])
        strategy = FunctionCallingStrategy()
        sink = InProcessEventSink("r1")
        step = await strategy.step(
            _ctx(), [Message(role="user", content="x")], await _client(provider), [], sink
        )
        assert step.kind == "finish"
        assert step.assistant_message.text == "All done"

    async def test_usage_accumulates_into_ctx(self):
        from jarvis.domain.message import Usage

        provider = MockModelProvider([turn("ok", usage=Usage(input_tokens=7, output_tokens=3))])
        strategy = FunctionCallingStrategy()
        ctx = _ctx()
        await strategy.step(
            ctx,
            [Message(role="user", content="x")],
            await _client(provider),
            [],
            InProcessEventSink("r1"),
        )
        assert ctx.usage.input_tokens == 7
        assert ctx.usage.output_tokens == 3

    async def test_rate_limit_retried_then_succeeds(self):
        provider = MockModelProvider(
            [
                turn(error=ModelRateLimitError("slow down", retry_after=0.01)),
                turn("recovered"),
            ]
        )
        strategy = FunctionCallingStrategy()
        sink = InProcessEventSink("r1")
        step = await strategy.step(
            _ctx(), [Message(role="user", content="x")], await _client(provider), [], sink
        )
        assert step.kind == "finish"
        attempts = [e.attempt for e in sink.events if e.type == "model.invocation.started"]
        assert attempts == [1, 2]

    async def test_rate_limit_exhausted_raises(self):
        provider = MockModelProvider(
            [
                turn(error=ModelRateLimitError("nope")),
                turn(error=ModelRateLimitError("still nope")),
            ]
        )
        strategy = FunctionCallingStrategy()
        with pytest.raises(ModelRateLimitError):
            await strategy.step(
                _ctx(),
                [Message(role="user", content="x")],
                await _client(provider),
                [],
                InProcessEventSink("r1"),
            )

    async def test_non_retryable_error_raises_immediately(self):
        provider = MockModelProvider(
            [
                turn(error=ModelBadRequestError("bad request")),
                turn("never reached"),
            ]
        )
        strategy = FunctionCallingStrategy()
        with pytest.raises(ModelBadRequestError):
            await strategy.step(
                _ctx(),
                [Message(role="user", content="x")],
                await _client(provider),
                [],
                InProcessEventSink("r1"),
            )
        assert provider.invocations == 1

    async def test_streaming_emits_text_deltas_and_accumulates_calls(self):
        from jarvis.domain.message import Usage
        from jarvis.models.types import FinishDelta, TextDelta, ToolCallDelta, UsageDelta

        provider = MockModelProvider(
            [
                turn(
                    deltas=[
                        TextDelta(text="Hel"),
                        TextDelta(text="lo"),
                        ToolCallDelta(index=0, id="c1", name="calculator"),
                        ToolCallDelta(index=0, arguments_fragment='{"e":'),
                        ToolCallDelta(index=0, arguments_fragment=" 1}"),
                        UsageDelta(usage=Usage(input_tokens=3, output_tokens=2)),
                        FinishDelta(finish_reason="tool_calls", model="mock-1"),
                    ]
                )
            ]
        )
        strategy = FunctionCallingStrategy()
        sink = InProcessEventSink("r1")
        ctx = _ctx()
        step = await strategy.step(
            ctx, [Message(role="user", content="x")], await _client(provider), TOOLS, sink
        )
        assert step.kind == "tool_calls"
        assert step.tool_calls == [ToolCall(id="c1", name="calculator", arguments={"e": 1})]
        deltas = [e.text for e in sink.events if e.type == "text.delta"]
        assert deltas == ["Hel", "lo"]
        assert ctx.usage.input_tokens == 3
        completed = [e for e in sink.events if e.type == "model.invocation.completed"]
        assert completed[0].finish_reason == "tool_calls"

    async def test_no_streaming_capability_uses_generate(self):
        provider = MockModelProvider(
            [turn("plain")],
            capabilities=ModelCapabilities(streaming=False),
        )
        strategy = FunctionCallingStrategy()
        sink = InProcessEventSink("r1")
        step = await strategy.step(
            _ctx(), [Message(role="user", content="x")], await _client(provider), [], sink
        )
        assert step.kind == "finish"
        assert [e.type for e in sink.events] == [
            "model.invocation.started",
            "model.invocation.completed",
        ]

    async def test_response_format_json_schema(self):
        provider = MockModelProvider([turn('{"a": 1}')])
        strategy = FunctionCallingStrategy()
        ctx = _ctx()
        ctx.output_schema = {"type": "object"}
        ctx.structured_mode = "json_schema"
        await strategy.step(
            ctx,
            [Message(role="user", content="x")],
            await _client(provider),
            [],
            InProcessEventSink("r1"),
        )
        assert provider.requests[0].response_format == {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": {"type": "object"}},
        }

    async def test_response_format_json_mode(self):
        provider = MockModelProvider([turn('{"a": 1}')])
        strategy = FunctionCallingStrategy()
        ctx = _ctx()
        ctx.output_schema = {"type": "object"}
        ctx.structured_mode = "json_mode"
        await strategy.step(
            ctx,
            [Message(role="user", content="x")],
            await _client(provider),
            [],
            InProcessEventSink("r1"),
        )
        assert provider.requests[0].response_format == {"type": "json_object"}

    async def test_no_output_schema_no_response_format(self):
        provider = MockModelProvider([turn("hi")])
        strategy = FunctionCallingStrategy()
        await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            [],
            InProcessEventSink("r1"),
        )
        assert provider.requests[0].response_format is None


class TestReAct:
    async def test_action_parsed(self):
        provider = MockModelProvider(
            [
                turn(
                    "Thought: I should calculate\n"
                    'Action: calculator\nAction Input: {"expression": "1+1"}'
                )
            ]
        )
        strategy = ReActStrategy()
        step = await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            TOOLS,
            InProcessEventSink("r1"),
        )
        assert step.kind == "tool_calls"
        assert step.tool_calls[0].name == "calculator"
        assert step.tool_calls[0].arguments == {"expression": "1+1"}

    async def test_final_answer_finishes(self):
        provider = MockModelProvider([turn("Thought: done\nFinal Answer: The result is 4")])
        strategy = ReActStrategy()
        step = await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            TOOLS,
            InProcessEventSink("r1"),
        )
        assert step.kind == "finish"

    async def test_malformed_reply_recovers_with_correction(self):
        provider = MockModelProvider([turn("I think the answer is probably four.")])
        strategy = ReActStrategy()
        step = await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            TOOLS,
            InProcessEventSink("r1"),
        )
        assert step.kind == "tool_calls"
        assert step.tool_calls == []
        assert len(step.messages) == 1
        assert step.messages[0].role == "developer"
        assert "expected format" in step.messages[0].content

    async def test_unknown_action_tool_recovers(self):
        provider = MockModelProvider(
            [turn("Thought: try\nAction: nonexistent_tool\nAction Input: {}")]
        )
        strategy = ReActStrategy()
        step = await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            TOOLS,
            InProcessEventSink("r1"),
        )
        assert step.tool_calls == []
        assert "not an available tool" in step.messages[0].content

    async def test_rate_limit_retried(self):
        provider = MockModelProvider(
            [
                turn(error=ModelRateLimitError("busy", retry_after=0.01)),
                turn("Thought: ok\nFinal Answer: fine"),
            ]
        )
        strategy = ReActStrategy()
        step = await strategy.step(
            _ctx(),
            [Message(role="user", content="x")],
            await _client(provider),
            [],
            InProcessEventSink("r1"),
        )
        assert step.kind == "finish"


class TestRegistry:
    def test_resolves_builtins(self):
        registry = DefaultStrategyRegistry()
        assert registry.resolve(StrategyConfig(type="function_calling")).name == "function_calling"
        assert registry.resolve(StrategyConfig(type="react")).name == "react"

    def test_unknown_strategy_raises(self):
        # StrategyConfig validates the literal, so construct past it to test the guard
        bogus = StrategyConfig.model_construct(type="bogus", params={})
        with pytest.raises(UnknownStrategyError):
            DefaultStrategyRegistry().resolve(bogus)
