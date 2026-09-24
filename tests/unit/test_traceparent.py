"""W3C traceparent (S7, ADR 0021 §6, D76) — inbound (a webhook's
`traceparent` joins the fired run to the producer's trace) and outbound
(http_get forwards the run's trace). Fakes + respx; no DB, no network."""

from __future__ import annotations

import respx

from jarvis.api.routes.triggers import _traceparent_trace_id
from jarvis.domain.message import ToolCall
from jarvis.domain.tools import ToolContext
from jarvis.runtime.trigger_firing import TriggerFireService
from jarvis.tools.builtin.http_get import HttpGetTool
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from tests.unit.test_trigger_firing import FakeAgents, FakeExecutions, _trigger, _version

VALID = "b" * 32


# --- inbound: the fire() kwarg ------------------------------------------------


async def test_fire_joins_the_callers_trace():
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    await TriggerFireService(executions=executions, agents=agents, run_timeout_seconds=120.0).fire(
        _trigger(), context_vars={}, trace_id=VALID
    )
    message = executions.written[0][1]
    assert message.trace_id == VALID


async def test_fire_with_invalid_trace_id_mints_fresh():
    for bad in ("", "short", "z" * 32, "0" * 32, None):
        agents, executions = FakeAgents([_version()]), FakeExecutions()
        await TriggerFireService(
            executions=executions, agents=agents, run_timeout_seconds=120.0
        ).fire(_trigger(), context_vars={}, trace_id=bad)
        trace = executions.written[0][1].trace_id
        assert len(trace) == 32 and trace != bad  # a fresh mint, never an error


async def test_fire_without_trace_id_mints_fresh():
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    await TriggerFireService(executions=executions, agents=agents, run_timeout_seconds=120.0).fire(
        _trigger(), context_vars={}
    )
    assert len(executions.written[0][1].trace_id) == 32


# --- inbound: the webhook header parse ----------------------------------------


def test_traceparent_parse_takes_the_trace_id():
    assert _traceparent_trace_id(f"00-{VALID}-{'c' * 16}-01") == VALID
    assert _traceparent_trace_id(f"03-{VALID.upper()}-{'c' * 16}-01") == VALID  # lowered


def test_traceparent_parse_rejects_malformed():
    assert _traceparent_trace_id(None) is None
    assert _traceparent_trace_id("") is None
    assert _traceparent_trace_id("00-not-a-trace") is None
    assert _traceparent_trace_id("00-short-cc-01") is None
    assert _traceparent_trace_id(f"00-{'z' * 32}-{'c' * 16}-01") is None
    assert _traceparent_trace_id(f"00-{'0' * 32}-{'c' * 16}-01") is None  # the all-zero null id


# --- outbound: http_get forwards the run's trace --------------------------------


@respx.mock
async def test_http_get_sends_traceparent_when_the_run_has_one():
    respx.get("https://api.example.com/").respond(200, text="ok")
    tool = HttpGetTool(config={"allowed_hosts": ["*"]})
    context = ToolContext(run_id="r1", agent_id="a1", trace_id=VALID)
    await tool.execute({"url": "https://api.example.com/"}, context)
    (route,) = respx.calls
    header = route.request.headers["traceparent"]
    version, trace, span, flags = header.split("-")
    assert version == "00" and trace == VALID and flags == "01"
    assert len(span) == 16


@respx.mock
async def test_http_get_omits_traceparent_without_a_trace():
    respx.get("https://api.example.com/").respond(200, text="ok")
    tool = HttpGetTool(config={"allowed_hosts": ["*"]})
    await tool.execute({"url": "https://api.example.com/"}, ToolContext(run_id="r1", agent_id="a1"))
    (route,) = respx.calls
    assert "traceparent" not in route.request.headers


async def test_tool_context_defaults_to_no_trace():
    assert ToolContext(run_id="r1", agent_id="a1").trace_id is None


@respx.mock
async def test_runtime_threads_the_run_trace_through():
    """The full tool path: the ToolRuntime envelope accepts a traced
    context (the runtime passes `trace_id` into every tool execution)."""
    respx.get("https://api.example.com/").respond(200, text="ok")
    registry = InMemoryToolRegistry()
    registry.register(HttpGetTool(config={"allowed_hosts": ["*"]}))
    result = await ToolRuntime(registry).execute(
        ToolCall(id="c", name="http_get", arguments={"url": "https://api.example.com/"}),
        ToolContext(run_id="r1", agent_id="a1", trace_id=VALID),
    )
    assert not result.is_error
