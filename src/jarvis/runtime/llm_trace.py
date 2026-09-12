"""Print-and-forget LLM trace (debug).

When `JARVIS_LLM_TRACE=true`, the runtime hands strategies an instrumented
client instead of the bare one: every model call's ACTUAL input — messages
including the system prompt (which is never persisted, S10) plus tool
schemas — and its output (text, tool calls, usage, finish reason) is logged
to the backend log, tagged with run id + the current iteration, AND recorded
into an in-memory `LlmTraceBuffer` that `GET /v1/executions/{run_id}/llm-trace`
serves to the web UI (ADR 0014). Nothing is persisted: no DB, no events — a
restart loses the buffer.

Payloads never carry secret material — credentials ride HTTP headers, never
the ModelRequest (ADR 0005/0006), so neither the log nor the buffer can leak
a key value.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken, ExecutionContext
from jarvis.domain.message import Message, ToolCall, Usage
from jarvis.models.capabilities import ModelCapabilities
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

logger = logging.getLogger("jarvis.llm_trace")


class LlmTraceBuffer:
    """Bounded in-memory trace registry keyed by run_id (ADR 0014) — the
    web-readable half of the debug trace, served by
    `GET /v1/executions/{run_id}/llm-trace`. Process-local by design: a
    restart loses it, and a distributed worker's buffer is invisible to
    the API route (the backend log is the distributed-mode trace)."""

    max_runs = 20

    def __init__(self) -> None:
        self._runs: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def record(self, run_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        entries = self._runs.get(run_id)
        if entries is None:
            entries = []
            self._runs[run_id] = entries
            while len(self._runs) > self.max_runs:
                self._runs.popitem(last=False)
        entries.append(entry)
        return entry

    def get(self, run_id: str) -> list[dict[str, Any]]:
        return list(self._runs.get(run_id, ()))


class TracedModelClient:
    """ModelClient by pure delegation, with one request/response log pair
    per call. Deltas pass through unmodified — streaming behavior is
    byte-identical; only the log differs. The strategy cannot tell the
    difference. With a `buffer`, the same payloads also land in the
    in-memory registry the trace route reads (ADR 0014)."""

    def __init__(
        self,
        client: ModelClient,
        ctx: ExecutionContext,
        buffer: LlmTraceBuffer | None = None,
    ) -> None:
        self._client = client
        self._ctx = ctx
        self._buffer = buffer
        self._pending: dict[str, Any] | None = None

    @property
    def ref(self) -> ModelRef:
        return self._client.ref

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._client.capabilities

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        self._log_request("generate", request)
        response = await self._client.generate(request, cancel=cancel)
        self._log_response("generate", response)
        return response

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        self._log_request("stream", request)
        return self._traced_stream(request, cancel=cancel)

    async def _traced_stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None
    ) -> AsyncIterator[StreamDelta]:
        # Accumulate exactly like function_calling._invoke_streaming so the
        # logged response is the response the strategy reconstructs.
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish_reason = "stop"
        model = request.model
        async for delta in self._client.stream(request, cancel=cancel):
            if isinstance(delta, TextDelta):
                text_parts.append(delta.text)
            elif isinstance(delta, ToolCallDelta):
                entry = tool_calls.setdefault(delta.index, {"arguments": ""})
                if delta.id:
                    entry["id"] = delta.id
                if delta.name:
                    entry["name"] = delta.name
                if delta.arguments_fragment:
                    entry["arguments"] += delta.arguments_fragment
            elif isinstance(delta, UsageDelta):
                usage = delta.usage
            elif isinstance(delta, FinishDelta):
                finish_reason = delta.finish_reason
                model = delta.model or request.model
            yield delta
        calls: list[ToolCall] | None = None
        if tool_calls:
            calls = []
            for index in sorted(tool_calls):
                entry = tool_calls[index]
                try:
                    arguments: dict[str, Any] = json.loads(entry.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": entry.get("arguments", "")}
                calls.append(
                    ToolCall(
                        id=entry.get("id") or f"call_{index}",
                        name=entry.get("name", ""),
                        arguments=arguments,
                    )
                )
        self._log_response(
            "stream",
            ModelResponse(
                message=Message(role="assistant", content="".join(text_parts), tool_calls=calls),
                usage=usage,
                finish_reason=finish_reason,
                model=model,
            ),
        )

    def _tag(self, method: str) -> str:
        ref = self._client.ref
        return (
            f"run {self._ctx.run_id} · iteration {self._ctx.iteration} · "
            f"{ref.provider}/{ref.model} · {method}"
        )

    def _log_request(self, method: str, request: ModelRequest) -> None:
        payload = request.model_dump(mode="json")
        logger.info("LLM request · %s\n%s", self._tag(method), _pretty(payload))
        if self._buffer is not None:
            # Recorded immediately, response filled in later — a failed call
            # still shows its exact request in the trace view.
            ref = self._client.ref
            self._pending = self._buffer.record(
                self._ctx.run_id,
                {
                    "iteration": self._ctx.iteration,
                    "method": method,
                    "provider": ref.provider,
                    "model": ref.model,
                    "at": _now_iso(),
                    "request": payload,
                    "response": None,
                },
            )

    def _log_response(self, method: str, response: ModelResponse) -> None:
        payload = response.model_dump(mode="json")
        logger.info(
            "LLM response · %s · finish=%s · tokens %d in / %d out\n%s",
            self._tag(method),
            response.finish_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            _pretty(payload),
        )
        if self._buffer is not None and self._pending is not None:
            self._pending["response"] = payload
            self._pending = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pretty(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
