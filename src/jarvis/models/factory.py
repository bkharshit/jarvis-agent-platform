"""ModelProviderFactory: resolve a ModelRef to a bound ModelClient.

`provider: "mock"` → MockModelProvider; anything else → the single
OpenAI-compatible adapter configured from the ref (base_url injection,
ADR 0005)."""

from __future__ import annotations

from typing import Any

from jarvis.domain.agent import ModelRef
from jarvis.models.client import BoundModelClient
from jarvis.models.mock import MockModelProvider
from jarvis.models.openai_compatible import OpenAICompatibleProvider


class DefaultModelProviderFactory:
    def __init__(
        self,
        *,
        mock_provider: MockModelProvider | None = None,
        api_key_override: str | None = None,
        extra_providers: dict[str, Any] | None = None,
    ) -> None:
        self._mock = mock_provider
        self._api_key_override = api_key_override
        self._extra = dict(extra_providers or {})

    def resolve(self, ref: ModelRef) -> BoundModelClient:
        if ref.provider == "mock":
            provider: Any = self._mock or MockModelProvider()
        elif ref.provider in self._extra:
            provider = self._extra[ref.provider]
        elif ref.provider == "openai_compatible" or ref.base_url:
            provider = OpenAICompatibleProvider.for_ref(ref, api_key=self._api_key_override)
        else:
            provider = OpenAICompatibleProvider.for_ref(
                ref,
                name=ref.provider,
                api_key=self._api_key_override,
            )
        return BoundModelClient(provider, ref)


__all__ = ["DefaultModelProviderFactory"]
