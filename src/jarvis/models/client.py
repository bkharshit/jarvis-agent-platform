"""BoundModelClient — a provider bound to a specific ModelRef.

Strategies only ever talk to a ModelClient (ports/model.py), never a raw
provider: the binding is where model name, base_url and api-key resolution
live."""

from __future__ import annotations

from collections.abc import AsyncIterator

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.types import ModelRequest, ModelResponse, StreamDelta
from jarvis.ports.model import ModelProvider


class BoundModelClient:
    def __init__(self, provider: ModelProvider, ref: ModelRef) -> None:
        self._provider = provider
        self._ref = ref

    @property
    def ref(self) -> ModelRef:
        return self._ref

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._provider.capabilities

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse:
        request = self._bind(request)
        return await self._provider.generate(request, cancel=cancel)

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]:
        async def _iterator() -> AsyncIterator[StreamDelta]:
            bound = self._bind(request)
            async for delta in self._provider.stream(bound, cancel=cancel):
                yield delta

        return _iterator()

    def _bind(self, request: ModelRequest) -> ModelRequest:
        if not request.model:
            return request.model_copy(update={"model": self._ref.model})
        return request


__all__ = ["BoundModelClient"]
