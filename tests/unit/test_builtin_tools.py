"""Builtin tools: calculator, current_time, http_get (respx-mocked)."""

from datetime import UTC, datetime

import respx

from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext
from jarvis.tools.builtin.calculator import CalculatorTool
from jarvis.tools.builtin.current_time import CurrentTimeTool
from jarvis.tools.builtin.http_get import HttpGetTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime


def _context(config=None):
    return ToolContext(run_id="r1", agent_id="a1", config=config or {})


def _runtime_with(*tools):
    registry = InMemoryToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolRuntime(registry)


class TestCalculator:
    async def test_basic_arithmetic(self):
        tool = CalculatorTool()
        assert await tool.execute({"expression": "2 + 3 * 4"}, _context()) == "14"
        assert await tool.execute({"expression": "(10 - 4) / 2"}, _context()) == "3"

    async def test_integer_float_normalized(self):
        tool = CalculatorTool()
        assert await tool.execute({"expression": "4 / 2"}, _context()) == "2"

    async def test_rejects_names(self):
        tool = CalculatorTool()
        result = await _runtime_with(tool).execute(
            ToolCall(id="c", name="calculator", arguments={"expression": "__import__('os')"}),
            _context(),
        )
        assert result.is_error

    async def test_validation_requires_expression(self):
        result = await _runtime_with(CalculatorTool()).execute(
            ToolCall(id="c", name="calculator", arguments={}), _context()
        )
        assert result.is_error
        assert result.metadata["kind"] == "validation"


class TestCurrentTime:
    async def test_utc_default(self):
        tool = CurrentTimeTool()
        output = await tool.execute({}, _context())
        parsed = datetime.fromisoformat(output)
        now = datetime.now(UTC)
        assert abs((parsed - now).total_seconds()) < 60

    async def test_named_timezone(self):
        tool = CurrentTimeTool()
        output = await tool.execute({"timezone": "Europe/Berlin"}, _context())
        assert "Europe/Berlin" in output or "+" in output  # offset present

    async def test_unknown_timezone_is_error(self):
        result = await _runtime_with(CurrentTimeTool()).execute(
            ToolCall(id="c", name="current_time", arguments={"timezone": "Mars/Olympus"}),
            _context(),
        )
        assert result.is_error
        assert "unknown timezone" in result.output


class TestHttpGet:
    @respx.mock
    async def test_allow_list_permits(self):
        respx.get("https://api.example.com/data").respond(200, text="payload")
        tool = HttpGetTool(config={"allowed_hosts": ["api.example.com"]})
        output = await tool.execute({"url": "https://api.example.com/data"}, _context())
        assert output.startswith("HTTP 200")
        assert "payload" in output

    @respx.mock
    async def test_allow_list_denies_other_hosts(self):
        respx.get("https://evil.example.com/x").respond(200, text="bad")
        tool = HttpGetTool(config={"allowed_hosts": ["api.example.com"]})
        result = await _runtime_with(tool).execute(
            ToolCall(id="c", name="http_get", arguments={"url": "https://evil.example.com/x"}),
            _context(),
        )
        assert result.is_error
        assert "not in the allow-list" in result.output

    async def test_empty_allow_list_refuses_everything(self):
        tool = HttpGetTool()
        result = await _runtime_with(tool).execute(
            ToolCall(id="c", name="http_get", arguments={"url": "https://api.example.com/"}),
            _context(),
        )
        assert result.is_error

    async def test_non_http_url_rejected(self):
        tool = HttpGetTool(config={"allowed_hosts": ["*"]})
        result = await _runtime_with(tool).execute(
            ToolCall(id="c", name="http_get", arguments={"url": "ftp://x/y"}), _context()
        )
        assert result.is_error
        assert "http(s)" in result.output

    @respx.mock
    async def test_body_truncated(self):
        respx.get("https://api.example.com/big").respond(200, text="y" * 50_000)
        tool = HttpGetTool(config={"allowed_hosts": ["api.example.com"], "max_chars": 100})
        output = await tool.execute({"url": "https://api.example.com/big"}, _context())
        assert len(output) < 500
        assert "truncated" in output
