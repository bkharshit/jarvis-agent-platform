"""LLM trace (print-and-forget, JARVIS_LLM_TRACE) — the runtime hands a
TracedModelClient to strategies only when the flag is on, and every model
call logs one request block + one response block tagged with run id and the
current iteration. Real runtime loop with the mock provider for the wiring
test; a duck-typed client for the wrapper tests. No DB, no network."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig
from jarvis.domain.execution import CancellationToken, ExecutionContext
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.errors import ModelError
from jarvis.models.mock import MockModelProvider, turn
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    StreamDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)
from jarvis.ports.model import ModelClient
from jarvis.runtime.agent_runtime import AgentRuntime
from jarvis.runtime.llm_trace import TracedModelClient
from jarvis.strategies.registry import DefaultStrategyRegistry
from jarvis.tools.registry import InMemoryToolRegistry
from jarvis.tools.runtime import ToolRuntime
from tests.unit.test_agent_runtime import _RecordingRepo

TRACE_LOGGER = "jarvis.llm_trace"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        run_id="run-trace", agent_id="a1", agent_version_id="v1", tenant_id="t1"
    )


def _request() -> ModelRequest:
    return ModelRequest(
        model="gemma4:31b",
        messages=[
            {"role": "system", "content": "You are a test agent."},
            {"role": "user", "content": "hello"},
        ],
    )


class FakeClient:
    """Duck-typed ModelClient: scripted deltas/exceptions, records calls."""

    ref = ModelRef(provider="ollama", model="gemma4:31b")
    capabilities = ModelCapabilities()

    def __init__(self, deltas: list[StreamDelta] | None = None, error: Exception | None = None):
        self._deltas = deltas or []
        self._error = error
        self.requests: list[ModelRequest] = []

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ModelResponse(
            message={"role": "assistant", "content": "the answer"},  # type: ignore[call-arg]
            usage={"input_tokens": 11, "output_tokens": 3, "extra": {}},  # type: ignore[call-arg]
        )

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        self.requests.append(request)

        async def gen() -> AsyncIterator[StreamDelta]:
            for delta in self._deltas:
                yield delta
            if self._error is not None:
                raise self._error

        return gen()


def _traced(client: ModelClient) -> TracedModelClient:
    return TracedModelClient(client, _ctx())


# --- the wrapper --------------------------------------------------------------


async def test_generate_logs_request_and_response_blocks(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    response = await _traced(FakeClient()).generate(_request())

    assert response.message.content == "the answer"  # delegation is intact
    request_lines = [r for r in caplog.records if "LLM request" in r.message]
    response_lines = [r for r in caplog.records if "LLM response" in r.message]
    assert len(request_lines) == len(response_lines) == 1
    request_text = request_lines[0].getMessage()
    assert "run run-trace · iteration 0 · ollama/gemma4:31b · generate" in request_text
    assert "You are a test agent." in request_text  # the system prompt — never persisted
    response_text = response_lines[0].getMessage()
    assert "the answer" in response_text
    assert "finish=stop · tokens 11 in / 3 out" in response_text


async def test_iteration_tag_is_read_at_call_time(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    ctx = _ctx()
    traced = TracedModelClient(FakeClient(), ctx)
    ctx.iteration = 4  # the loop mutates ctx between calls; tag must follow
    await traced.generate(_request())
    assert "iteration 4" in caplog.records[0].getMessage()


async def test_stream_logs_one_pair_and_passes_deltas_through(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    deltas: list[StreamDelta] = [
        TextDelta(text="Hel"),
        TextDelta(text="lo"),
        ToolCallDelta(index=0, id="call-1", name="calc", arguments_fragment='{"x"'),
        ToolCallDelta(index=0, arguments_fragment=": 1}"),
        UsageDelta(usage={"input_tokens": 7, "output_tokens": 5, "extra": {}}),  # type: ignore[call-arg]
        FinishDelta(model="gemma4:31b"),
    ]
    client = FakeClient(deltas=deltas)
    seen = [delta async for delta in _traced(client).stream(_request())]

    assert seen == deltas  # byte-identical pass-through
    request_lines = [r for r in caplog.records if "LLM request" in r.message]
    response_lines = [r for r in caplog.records if "LLM response" in r.message]
    assert len(request_lines) == len(response_lines) == 1  # ONE response block
    response_text = response_lines[0].getMessage()
    assert "Hello" in response_text  # text joined
    assert '"name": "calc"' in response_text and '"x": 1' in response_text  # fragments rejoined
    assert "tokens 7 in / 5 out" in response_text


async def test_generate_error_propagates_and_still_logs_the_request(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    with pytest.raises(ModelError):
        await _traced(FakeClient(error=ModelError("boom", provider="p", model="m"))).generate(
            _request()
        )
    assert len([r for r in caplog.records if "LLM request" in r.message]) == 1
    assert len([r for r in caplog.records if "LLM response" in r.message]) == 0


async def test_stream_error_propagates_without_a_response_block(
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    client = FakeClient(
        deltas=[TextDelta(text="partial")], error=ModelError("boom", provider="p", model="m")
    )
    with pytest.raises(ModelError):
        async for _ in _traced(client).stream(_request()):
            pass
    assert len([r for r in caplog.records if "LLM request" in r.message]) == 1
    assert len([r for r in caplog.records if "LLM response" in r.message]) == 0


# --- the runtime wiring -------------------------------------------------------


def _agent() -> AgentDefinition:
    return AgentDefinition(
        id="a1",
        name="demo",
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        system_prompt="You are a test agent.",
    )


def _version(agent: AgentDefinition) -> AgentVersion:
    from uuid import uuid4

    return AgentVersion(id=str(uuid4()), agent_id=agent.id, version=1, snapshot=agent)


def _runtime(provider: MockModelProvider, *, trace_llm: bool) -> AgentRuntime:
    base = InMemoryToolRegistry()
    repo = _RecordingRepo()
    return AgentRuntime(
        strategies=DefaultStrategyRegistry(),
        tools=base,
        tool_runtime=ToolRuntime(base),
        models=_mock_factory(provider),
        conversations=repo,
        executions=repo,
        trace_llm=trace_llm,
    )


def _mock_factory(provider: MockModelProvider):
    from jarvis.models.factory import DefaultModelProviderFactory

    return DefaultModelProviderFactory(mock_provider=provider)


async def test_runtime_with_the_flag_traces_real_runs(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    runtime = _runtime(MockModelProvider([turn("hi there")]), trace_llm=True)

    result = await runtime.run(_version(_agent()), "hello", _ctx())

    assert result.status == "succeeded"
    request_lines = [r for r in caplog.records if "LLM request" in r.message]
    assert len(request_lines) == 1  # one iteration, one call
    request_text = request_lines[0].getMessage()
    assert f"run {result.run_id}" in request_text
    assert "You are a test agent." in request_text  # system prompt is visible at last


async def test_runtime_without_the_flag_stays_silent(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    runtime = _runtime(MockModelProvider([turn("hi there")]), trace_llm=False)

    result = await runtime.run(_version(_agent()), "hello", _ctx())

    assert result.status == "succeeded"
    assert not [r for r in caplog.records if r.name == TRACE_LOGGER]
