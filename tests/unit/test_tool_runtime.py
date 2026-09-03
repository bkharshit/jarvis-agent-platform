"""ToolRuntime: validation, timeout, never-crash, cancellation."""

import asyncio

import pytest

from jarvis.domain.execution import ExecutionCancelled
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext, ToolDescriptor
from jarvis.tools.base import BaseTool
from jarvis.tools.registry import DuplicateToolError, InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


class _EchoTool(BaseTool):
    def __init__(self, schema=None):
        super().__init__(
            ToolDescriptor(
                name="echo",
                description="echo",
                parameters=schema or {"type": "object", "properties": {"text": {"type": "string"}}},
            )
        )

    async def _execute(self, arguments, context):
        return str(arguments.get("text", ""))


class _BoomTool(BaseTool):
    def __init__(self):
        super().__init__(ToolDescriptor(name="boom", description="explode", parameters={}))

    async def _execute(self, arguments, context):
        raise RuntimeError("kaboom")


class _SlowTool(BaseTool):
    def __init__(self, seconds=5.0):
        super().__init__(ToolDescriptor(name="slow", description="sleep", parameters={}))
        self.seconds = seconds

    async def _execute(self, arguments, context):
        await asyncio.sleep(self.seconds)
        return "done"


class _CancellingTool(BaseTool):
    def __init__(self):
        super().__init__(ToolDescriptor(name="cancellable", description="waits", parameters={}))

    async def _execute(self, arguments, context):
        await context.cancel.wait()
        context.raise_if_cancelled()
        return "never"


def _runtime(tools, **kw) -> ToolRuntime:
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolRuntime(registry, **kw)


def _context(**kw) -> ToolContext:
    return ToolContext(run_id="r1", agent_id="a1", **kw)


class TestRegistry:
    def test_duplicate_registration_raises(self):
        registry = InMemoryToolRegistry()
        registry.register(_EchoTool())
        with pytest.raises(DuplicateToolError):
            registry.register(_EchoTool())

    def test_get_unknown_raises(self):
        from jarvis.tools.registry import ToolNotFoundError

        with pytest.raises(ToolNotFoundError):
            InMemoryToolRegistry().get("nope")


class TestValidation:
    async def test_valid_arguments_pass(self):
        runtime = _runtime([_EchoTool()])
        result = await runtime.execute(
            ToolCall(id="c1", name="echo", arguments={"text": "hi"}), _context()
        )
        assert result.output == "hi"
        assert not result.is_error

    async def test_wrong_type_is_validation_error(self):
        runtime = _runtime([_EchoTool()])
        result = await runtime.execute(
            ToolCall(id="c1", name="echo", arguments={"text": 42}), _context()
        )
        assert result.is_error
        assert result.metadata["kind"] == "validation"
        assert "invalid arguments" in result.output

    async def test_unknown_tool_is_error_result_not_crash(self):
        runtime = _runtime([])
        result = await runtime.execute(ToolCall(id="c1", name="echo", arguments={}), _context())
        assert result.is_error
        assert result.metadata["kind"] == "internal"
        assert "unknown tool" in result.output


class TestNeverCrash:
    async def test_tool_exception_becomes_error_result(self):
        runtime = _runtime([_BoomTool()])
        result = await runtime.execute(ToolCall(id="c1", name="boom", arguments={}), _context())
        assert result.is_error
        assert result.metadata["kind"] == "internal"
        assert "RuntimeError" in result.output


class TestTimeout:
    async def test_timeout_produces_timeout_result(self):
        runtime = _runtime([_SlowTool(seconds=5)], default_timeout=0.05)
        result = await runtime.execute(ToolCall(id="c1", name="slow", arguments={}), _context())
        assert result.is_error
        assert result.metadata["kind"] == "timeout"

    async def test_context_config_timeout_wins(self):
        runtime = _runtime([_SlowTool(seconds=5)], default_timeout=10)
        result = await runtime.execute(
            ToolCall(id="c1", name="slow", arguments={}),
            _context(config={"timeout": 0.05}),
        )
        assert result.metadata["kind"] == "timeout"


class TestCancellation:
    async def test_cancelled_tool_propagates_cancellation(self):
        runtime = _runtime([_CancellingTool()])
        context = _context()

        async def _cancel_soon():
            await asyncio.sleep(0.02)
            context.cancel.trigger("user stop")

        asyncio.ensure_future(_cancel_soon())
        with pytest.raises(ExecutionCancelled):
            await runtime.execute(ToolCall(id="c1", name="cancellable", arguments={}), context)

    async def test_pre_cancelled_context_raises_immediately(self):
        runtime = _runtime([_SlowTool(seconds=0)])
        context = _context()
        context.cancel.trigger("already gone")
        with pytest.raises(ExecutionCancelled):
            await runtime.execute(ToolCall(id="c1", name="slow", arguments={}), context)


class TestOutputGuard:
    async def test_long_output_truncated(self):
        class _VerboseTool(BaseTool):
            def __init__(self):
                super().__init__(ToolDescriptor(name="verbose", description="v", parameters={}))

            async def _execute(self, arguments, context):
                return "x" * 100_000

        runtime = _runtime([_VerboseTool()], max_output_chars=1000)
        result = await runtime.execute(ToolCall(id="c1", name="verbose", arguments={}), _context())
        assert len(result.output) < 100_000
        assert "truncated" in result.output
