"""Model access protocols — Protocols ONLY (pydantic/stdlib imports only).

ADR 0005: generate and stream are separate methods; capabilities are
declared, not discovered at runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from jarvis.domain.agent import ModelRef
from jarvis.domain.execution import CancellationToken
from jarvis.models.capabilities import ModelCapabilities
from jarvis.models.errors import ModelError
from jarvis.models.types import ModelRequest, ModelResponse, StreamDelta


@runtime_checkable
class ModelProvider(Protocol):
    """A concrete provider (openai_compatible, mock, future native ones)."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]: ...

    async def list_models(self) -> list[str]:
        """Model ids this endpoint serves (ADR 0007). Raises ModelError on
        failure — a provider that cannot enumerate says so, never pretends."""
        ...


@runtime_checkable
class ModelClient(Protocol):
    """A provider bound to a specific ModelRef (model name, base_url,
    api_key env). Strategies call this, never the provider directly."""

    @property
    def ref(self) -> ModelRef: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    async def generate(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, *, cancel: CancellationToken | None = None
    ) -> AsyncIterator[StreamDelta]: ...


class ModelProviderFactory(Protocol):
    """Resolves a ModelRef to a bound client. Raises ModelError on unknown
    provider."""

    def resolve(self, ref: ModelRef) -> ModelClient: ...

    async def list_models(
        self,
        provider: str,
        *,
        base_url: str | None = None,
        api_key_env: str | None = None,
    ) -> list[str]:
        """Live catalog for a provider (ADR 0007). Absent base_url/api_key_env
        fall back to environment defaults. Raises ModelError on unknown
        provider or failed listing."""
        ...


__all__ = [
    "ModelError",
    "ModelClient",
    "ModelProvider",
    "ModelProviderFactory",
]
