"""OpenAI-compatible provider — one adapter via base_url injection (ADR 0005).

Plain httpx against `{base_url}/chat/completions`. Serves OpenAI, Ollama
(`http://localhost:11434/v1`), vLLM, LM Studio. generate/stream are separate
methods; cancellation wraps the HTTP call in an asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.errors import (
    ModelAbortedError,
    ModelAuthError,
    ModelBadRequestError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelStreamError,
)
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    StreamDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
)


class OpenAICompatibleProvider:
    """Stateless per-request transport; one instance may serve many refs via
    generate_for/stream_for, but the ModelProvider protocol binds a ref at
    construction (the factory does that)."""

    def __init__(
        self,
        *,
        name: str = "openai_compatible",
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str | None = None,
        api_key: str | None = None,
        capabilities: ModelCapabilities | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._api_key = api_key
        self._timeout = timeout
        self.capabilities = capabilities or ModelCapabilities()

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    def for_ref(cls, ref: ModelRef, **kwargs: Any) -> OpenAICompatibleProvider:
        return cls(
            base_url=ref.base_url or "https://api.openai.com/v1",
            api_key_env=ref.api_key_env,
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        api_key = self._api_key
        if api_key is None and self._api_key_env:
            api_key = os.environ.get(self._api_key_env)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    # --- protocol methods (ref already bound via base_url/api_key) --------

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        payload = self._request_payload(request, stream=False)
        data = await self._post(payload, cancel)
        try:
            choice = data["choices"][0]
            raw_message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelStreamError(
                f"malformed response payload: {exc}", provider=self._name, model=request.model
            ) from exc
        usage = _parse_usage(data.get("usage"))
        return ModelResponse(
            message=_parse_message(raw_message),
            usage=usage,
            finish_reason=choice.get("finish_reason", "stop"),
            model=data.get("model", request.model),
        )

    async def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        payload = self._request_payload(request, stream=True)
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await self._send_with_cancel(
                client.build_request(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                ),
                client,
                cancel,
            )
            if response.status_code != 200:
                self._raise_http_error(response.status_code, response.text, request.model)
            buffer: dict[int, dict[str, Any]] = {}
            finish_reason = "stop"
            model = request.model
            usage = Usage()
            async for line in response.aiter_lines():
                if cancel is not None and cancel.triggered:
                    raise ModelAbortedError(
                        "cancelled mid-stream", provider=self._name, model=request.model
                    )
                if not line.startswith("data:"):
                    continue
                chunk = line.removeprefix("data:").strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError as exc:
                    raise ModelStreamError(
                        f"malformed SSE chunk: {exc}", provider=self._name, model=request.model
                    ) from exc
                delta = (data.get("choices") or [{}])[0].get("delta", {})
                if content := delta.get("content"):
                    yield TextDelta(text=content)
                for raw_call in delta.get("tool_calls") or []:
                    index = raw_call.get("index", 0)
                    entry = buffer.setdefault(index, {})
                    if call_id := raw_call.get("id"):
                        entry["id"] = call_id
                    function = raw_call.get("function") or {}
                    if fn_name := function.get("name"):
                        entry["name"] = fn_name
                    if arguments := function.get("arguments"):
                        entry.setdefault("arguments", "")
                        entry["arguments"] += arguments
                    yield ToolCallDelta(
                        index=index,
                        id=entry.get("id"),
                        name=entry.get("name"),
                        arguments_fragment=function.get("arguments") or "",
                    )
                if len(data.get("choices") or []) and data["choices"][0].get("finish_reason"):
                    finish_reason = data["choices"][0]["finish_reason"]
                if data.get("model"):
                    model = data["model"]
                if data.get("usage"):
                    usage = _parse_usage(data["usage"])
            if usage.input_tokens or usage.output_tokens:
                yield UsageDelta(usage=usage)
            yield FinishDelta(finish_reason=finish_reason, model=model)
        finally:
            await client.aclose()

    # --- internals ---------------------------------------------------------

    def _request_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_payload(m) for m in request.messages],
        }
        if request.tools and self.capabilities.function_calling:
            payload["tools"] = [_tool_payload(t) for t in request.tools]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.stop:
            payload["stop"] = request.stop
        if stream:
            payload["stream"] = True
            if self.capabilities.structured_output == "json_schema":
                payload["stream_options"] = {"include_usage": True}
        return payload

    async def _post(
        self, payload: dict[str, Any], cancel: CancellationToken | None
    ) -> dict[str, Any]:
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            request = client.build_request(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            response = await self._send_with_cancel(request, client, cancel)
            if response.status_code != 200:
                model_name = str(payload.get("model", ""))
                self._raise_http_error(response.status_code, response.text, model_name)
            data: dict[str, Any] = response.json()
            return data
        finally:
            await client.aclose()

    async def _send_with_cancel(
        self,
        request: httpx.Request,
        client: httpx.AsyncClient,
        cancel: CancellationToken | None,
    ) -> httpx.Response:
        send_task = asyncio.ensure_future(client.send(request))
        if cancel is None:
            return await send_task
        cancel_task = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {send_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            send_task.cancel()
            raise
        finally:
            cancel_task.cancel()
        if send_task in done:
            return send_task.result()
        send_task.cancel()
        try:
            await send_task
        except (asyncio.CancelledError, httpx.RequestError):
            pass
        raise ModelAbortedError("cancelled during invocation", provider=self._name)

    def _raise_http_error(self, status_code: int, body: str, model: str) -> None:
        message = body.strip()[:500] or f"HTTP {status_code}"
        if status_code in (401, 403):
            raise ModelAuthError(message, provider=self._name, model=model, status_code=status_code)
        if status_code == 429:
            raise ModelRateLimitError(
                message, provider=self._name, model=model, status_code=status_code
            )
        if status_code >= 500:
            raise ModelConnectionError(
                message, provider=self._name, model=model, status_code=status_code
            )
        raise ModelBadRequestError(
            message, provider=self._name, model=model, status_code=status_code
        )


def _message_payload(message: Message) -> dict[str, Any]:
    content = message.content if isinstance(message.content, str) else message.text
    payload: dict[str, Any] = {"role": message.role, "content": content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    return payload


def _tool_payload(descriptor: ToolDescriptor) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": descriptor.name,
            "description": descriptor.description,
            "parameters": descriptor.parameters or {"type": "object", "properties": {}},
        },
    }


def _parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=int(raw.get("prompt_tokens", 0)),
        output_tokens=int(raw.get("completion_tokens", 0)),
    )


def _parse_message(raw: dict[str, Any]) -> Message:
    tool_calls: list[ToolCall] | None = None
    if raw.get("tool_calls"):
        calls = []
        for entry in raw["tool_calls"]:
            function = entry.get("function", {})
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            calls.append(
                ToolCall(
                    id=entry.get("id", ""),
                    name=function.get("name", ""),
                    arguments=arguments or {},
                )
            )
        tool_calls = calls
    return Message(role="assistant", content=raw.get("content") or "", tool_calls=tool_calls)


__all__ = ["OpenAICompatibleProvider"]
