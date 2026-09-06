"""OpenAI-compatible adapter: translation, error mapping, cancellation.

httpx is mocked with respx — zero network."""

import asyncio
import json

import httpx
import pytest
import respx

from jarvis.domain.agent import EnvCredentialRef, ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.domain.message import Message, ToolCall
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.errors import (
    ModelAbortedError,
    ModelAuthError,
    ModelBadRequestError,
    ModelRateLimitError,
)
from jarvis.models.openai_compatible import OpenAICompatibleProvider
from jarvis.models.types import FinishDelta, ModelRequest, TextDelta, UsageDelta

BASE = "http://localhost:9999/v1"
URL = f"{BASE}/chat/completions"


def _provider(**kw) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(base_url=BASE, **kw)


def _request(**kw) -> ModelRequest:
    defaults = dict(
        model="test-model",
        messages=[Message(role="user", content="hello")],
        temperature=0.3,
    )
    defaults.update(kw)
    return ModelRequest(**defaults)


def _response_body(content="hi", tool_calls=None, finish_reason="stop"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
        "model": "test-model",
    }


class TestRequestTranslation:
    @respx.mock
    async def test_generate_translates_request_and_response(self):
        route = respx.post(URL).respond(json=_response_body("The answer"))
        result = await _provider().generate(_request())
        assert result.message.text == "The answer"
        assert result.usage.input_tokens == 12
        assert result.usage.output_tokens == 34
        assert result.finish_reason == "stop"

        sent = json.loads(route.calls.last.request.content)
        assert sent["model"] == "test-model"
        assert sent["temperature"] == 0.3
        assert sent["messages"] == [{"role": "user", "content": "hello"}]
        assert "tools" not in sent
        assert "stream" not in sent

    @respx.mock
    async def test_tools_translated_to_function_schema(self):
        route = respx.post(URL).respond(json=_response_body())
        descriptor = ToolDescriptor(
            name="calculator",
            description="math",
            parameters={"type": "object", "properties": {"expr": {"type": "string"}}},
        )
        await _provider().generate(_request(tools=[descriptor]))
        sent = json.loads(route.calls.last.request.content)
        assert sent["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "math",
                    "parameters": {
                        "type": "object",
                        "properties": {"expr": {"type": "string"}},
                    },
                },
            }
        ]

    @respx.mock
    async def test_tools_omitted_without_function_calling(self):
        from jarvis.models.capabilities import ModelCapabilities

        route = respx.post(URL).respond(json=_response_body())
        provider = OpenAICompatibleProvider(
            base_url=BASE,
            capabilities=ModelCapabilities(function_calling=False),
        )
        descriptor = ToolDescriptor(name="x", description="y", parameters={})
        await provider.generate(_request(tools=[descriptor]))
        sent = json.loads(route.calls.last.request.content)
        assert "tools" not in sent

    @respx.mock
    async def test_tool_calls_parsed_from_response(self):
        body = _response_body(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calc", "arguments": '{"expr": "1+1"}'},
                }
            ]
        )
        respx.post(URL).respond(json=body)
        result = await _provider().generate(_request())
        assert result.message.tool_calls == [
            ToolCall(id="call_1", name="calc", arguments={"expr": "1+1"})
        ]

    @respx.mock
    async def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "secret123")
        route = respx.post(URL).respond(json=_response_body())
        provider = OpenAICompatibleProvider(base_url=BASE, api_key_env="TEST_KEY")
        await provider.generate(_request())
        assert route.calls.last.request.headers["Authorization"] == "Bearer secret123"

    @respx.mock
    async def test_base_url_injection(self):
        # Ollama-style base_url works identically — same code path, different host
        route = respx.post("http://localhost:11434/v1/chat/completions").respond(
            json=_response_body()
        )
        provider = OpenAICompatibleProvider(base_url="http://localhost:11434/v1/")
        result = await provider.generate(_request())
        assert result.message.text == "hi"
        assert route.called


class TestErrorMapping:
    @respx.mock
    async def test_auth_error(self):
        respx.post(URL).respond(status_code=401, text="bad key")
        with pytest.raises(ModelAuthError) as exc_info:
            await _provider().generate(_request())
        assert exc_info.value.status_code == 401

    @respx.mock
    async def test_rate_limit_error(self):
        respx.post(URL).respond(status_code=429, text="slow down")
        with pytest.raises(ModelRateLimitError):
            await _provider().generate(_request())

    @respx.mock
    async def test_bad_request_error(self):
        respx.post(URL).respond(status_code=400, text="bad")
        with pytest.raises(ModelBadRequestError):
            await _provider().generate(_request())

    @respx.mock
    async def test_malformed_payload_raises_stream_error(self):
        from jarvis.models.errors import ModelStreamError

        respx.post(URL).respond(json={"unexpected": True})
        with pytest.raises(ModelStreamError):
            await _provider().generate(_request())


class TestCancellation:
    async def test_cancel_before_invocation(self):
        token = CancellationToken()
        token.trigger("user stop")
        with pytest.raises(ModelAbortedError):
            await _provider().generate(_request(), cancel=token)

    @respx.mock
    async def test_cancel_during_invocation(self):
        token = CancellationToken()

        def _hang(request):
            async def _never_respond():
                await asyncio.sleep(30)
                return httpx.Response(200, json=_response_body())

            return _never_respond()

        respx.post(URL).mock(side_effect=_hang)

        async def _cancel_soon():
            await asyncio.sleep(0.05)
            token.trigger("user stop")

        asyncio.ensure_future(_cancel_soon())
        with pytest.raises(ModelAbortedError):
            await _provider().generate(_request(), cancel=token)


def _sse(*payloads, done: bool = True) -> str:
    """Build an SSE body from JSON payloads (plus a terminating [DONE])."""
    lines = [f"data: {json.dumps(payload)}" for payload in payloads]
    if done:
        lines.append("data: [DONE]")
    return "\n".join(lines)


class TestStreaming:
    @respx.mock
    async def test_stream_parses_sse_chunks(self):
        sse = _sse(
            {"choices": [{"delta": {"content": "Hel"}}], "model": "test-model"},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "model": "test-model"},
        )
        respx.post(URL).respond(
            status_code=200, text=sse, headers={"Content-Type": "text/event-stream"}
        )

        provider = _provider()
        deltas = [delta async for delta in provider.stream(_request())]

        texts = [d.text for d in deltas if isinstance(d, TextDelta)]
        assert texts == ["Hel", "lo"]
        finish = [d for d in deltas if isinstance(d, FinishDelta)]
        assert finish[0].finish_reason == "stop"
        assert finish[0].model == "test-model"

    @respx.mock
    async def test_stream_accumulates_tool_call_fragments(self):
        from jarvis.models.types import ToolCallDelta

        def _tool_delta(fragment):
            return {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": fragment}}]}}
                ]
            }

        first_call = {"index": 0, "id": "c1", "function": {"name": "calc"}}
        sse = _sse(
            {"choices": [{"delta": {"tool_calls": [first_call]}}]},
            _tool_delta('{"expr"'),
            _tool_delta(': "1+1"}'),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
        respx.post(URL).respond(
            status_code=200, text=sse, headers={"Content-Type": "text/event-stream"}
        )
        provider = _provider()
        deltas = [delta async for delta in provider.stream(_request())]
        fragments = "".join(d.arguments_fragment for d in deltas if isinstance(d, ToolCallDelta))
        assert fragments == '{"expr": "1+1"}'
        finish = [d for d in deltas if isinstance(d, FinishDelta)]
        assert finish[0].finish_reason == "tool_calls"

    @respx.mock
    async def test_stream_emits_usage_delta(self):
        sse = _sse(
            {"choices": [{"delta": {"content": "x"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )
        respx.post(URL).respond(
            status_code=200, text=sse, headers={"Content-Type": "text/event-stream"}
        )
        deltas = [d async for d in _provider().stream(_request())]
        usage = [d for d in deltas if isinstance(d, UsageDelta)]
        assert usage[0].usage.input_tokens == 5
        assert usage[0].usage.output_tokens == 7


class TestListModels:
    @respx.mock
    async def test_lists_sorted_ids(self):
        respx.get(f"{BASE}/models").respond(
            status_code=200,
            json={"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": 42}, "junk"]},
        )
        assert await _provider().list_models() == ["alpha", "zeta"]

    @respx.mock
    async def test_sends_auth_header_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_LIST_KEY", "sk-list")
        route = respx.get(f"{BASE}/models").respond(status_code=200, json={"data": [{"id": "m"}]})
        await _provider(api_key_env="TEST_LIST_KEY").list_models()
        assert route.calls.last.request.headers["Authorization"] == "Bearer sk-list"

    @respx.mock
    async def test_auth_error_mapped(self):
        respx.get(f"{BASE}/models").respond(status_code=401, text="nope")
        with pytest.raises(ModelAuthError):
            await _provider().list_models()

    @respx.mock
    async def test_connection_error_mapped(self):
        respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
        from jarvis.models.errors import ModelConnectionError

        with pytest.raises(ModelConnectionError):
            await _provider().list_models()

    @respx.mock
    async def test_malformed_payload_raises_stream_error(self):
        respx.get(f"{BASE}/models").respond(status_code=200, json={"models": ["x"]})
        from jarvis.models.errors import ModelStreamError

        with pytest.raises(ModelStreamError):
            await _provider().list_models()


class TestFactory:
    async def test_mock_ref_resolves_mock(self):
        from jarvis.models.factory import DefaultModelProviderFactory
        from jarvis.models.mock import MockModelProvider

        factory = DefaultModelProviderFactory()
        client = factory.resolve(ModelRef(provider="mock", model="m1"))
        result = await client.generate(
            ModelRequest(model="", messages=[Message(role="user", content="hi")])
        )
        assert result.message.text == "This is a mock response."
        assert isinstance(client._provider, MockModelProvider)  # noqa: SLF001

    async def test_openai_ref_resolves_adapter(self):
        from jarvis.models.factory import DefaultModelProviderFactory

        factory = DefaultModelProviderFactory()
        client = factory.resolve(
            ModelRef(provider="openai_compatible", model="gpt-x", base_url=BASE)
        )
        assert client.ref.model == "gpt-x"

    async def test_list_models_delegates_to_mock(self):
        from jarvis.models.factory import DefaultModelProviderFactory

        factory = DefaultModelProviderFactory()
        assert await factory.list_models("mock") == ["mock-small", "mock-large"]

    @respx.mock
    async def test_list_models_builds_adapter_from_params(self):
        from jarvis.models.factory import DefaultModelProviderFactory

        route = respx.get(f"{BASE}/models").respond(
            status_code=200, json={"data": [{"id": "gemma4:31b"}]}
        )
        factory = DefaultModelProviderFactory()
        assert await factory.list_models(
            "openai_compatible",
            base_url=BASE,
            credential_ref=EnvCredentialRef(type="env", env_var="TEST_LIST_KEY"),
        ) == ["gemma4:31b"]
        assert route.called

    async def test_list_models_extra_provider_without_listing(self):
        from jarvis.models.errors import ModelError
        from jarvis.models.factory import DefaultModelProviderFactory

        factory = DefaultModelProviderFactory(extra_providers={"stub": object()})
        with pytest.raises(ModelError):
            await factory.list_models("stub")
