"""ModelProviderFactory: resolve a ModelRef to a bound ModelClient.

`provider: "mock"` → MockModelProvider; anything else → the single
OpenAI-compatible adapter configured from the ref (base_url injection,
ADR 0005). Credential references materialize through the CredentialResolver
(ADR 0006): env refs stay lazy (D18), stored refs resolve tenant-scoped to
short-lived material that lives only inside the per-resolve provider
instance."""

from __future__ import annotations

from typing import Any

from jarvis.domain.agent import CredentialRef, ModelRef, StoredCredentialRef
from jarvis.domain.auth import Principal
from jarvis.models.client import BoundModelClient
from jarvis.models.credentials import DefaultCredentialResolver
from jarvis.models.errors import ModelAuthError, ModelError
from jarvis.models.mock import MockModelProvider
from jarvis.models.openai_compatible import OpenAICompatibleProvider
from jarvis.ports.credential import (
    CredentialError,
    ResolvedEnv,
    ResolvedMaterial,
)


class DefaultModelProviderFactory:
    def __init__(
        self,
        *,
        mock_provider: MockModelProvider | None = None,
        api_key_override: str | None = None,
        extra_providers: dict[str, Any] | None = None,
        credential_resolver: DefaultCredentialResolver | Any | None = None,
    ) -> None:
        self._mock = mock_provider
        self._api_key_override = api_key_override
        self._extra = dict(extra_providers or {})
        self._credentials = credential_resolver or DefaultCredentialResolver()

    def resolve(self, ref: ModelRef, *, principal: Principal | None = None) -> BoundModelClient:
        if ref.provider == "mock":
            provider: Any = self._mock or MockModelProvider()
        elif ref.provider in self._extra:
            provider = self._extra[ref.provider]
        elif isinstance(ref.credential_ref, StoredCredentialRef):
            provider = self._resolve_with_material(ref, ref.credential_ref, principal)
        elif ref.provider == "openai_compatible" or ref.base_url:
            provider = OpenAICompatibleProvider.for_ref(ref, api_key=self._api_key_override)
        else:
            provider = OpenAICompatibleProvider.for_ref(
                ref,
                name=ref.provider,
                api_key=self._api_key_override,
            )
        return BoundModelClient(provider, ref)

    def _resolve_with_material(
        self,
        ref: ModelRef,
        credential: StoredCredentialRef,
        principal: Principal | None,
    ) -> OpenAICompatibleProvider:
        """Stored references materialize here — the only place key material
        exists, and only for the lifetime of this provider instance."""
        if principal is None:
            raise ModelAuthError(
                "stored credential resolution requires a principal — "
                "a credential id alone is never sufficient authorization",
                provider=ref.provider,
                model=ref.model,
            )
        try:
            resolved = self._credentials.resolve(principal, credential)
        except CredentialError as exc:
            raise ModelAuthError(exc.message, provider=ref.provider, model=ref.model) from exc
        if not isinstance(resolved, ResolvedMaterial):
            raise ModelError(
                "stored credential resolved to an environment reference",
                provider=ref.provider,
                model=ref.model,
            )
        return OpenAICompatibleProvider.for_ref(ref, api_key=resolved.value)

    async def list_models(
        self,
        provider: str,
        *,
        base_url: str | None = None,
        credential_ref: CredentialRef | None = None,
        principal: Principal | None = None,
    ) -> list[str]:
        """Live catalog (ADR 0007). The route never reaches past the factory
        into adapter constructors."""
        if provider == "mock":
            instance: MockModelProvider | Any = self._mock or MockModelProvider()
        elif provider in self._extra:
            instance = self._extra[provider]
            if not hasattr(instance, "list_models"):
                raise ModelError(
                    f"provider {provider!r} does not support model listing",
                    provider=provider,
                )
        else:
            api_key = self._api_key_override
            api_key_env = None
            if credential_ref is not None:
                try:
                    resolved = self._credentials.resolve(principal, credential_ref)
                except CredentialError as exc:
                    raise ModelAuthError(exc.message, provider=provider) from exc
                if isinstance(resolved, ResolvedMaterial):
                    api_key = resolved.value
                elif isinstance(resolved, ResolvedEnv):
                    api_key_env = resolved.env_var
                else:  # pragma: no cover — discriminated union is exhaustive
                    raise ModelError("unknown resolved credential kind", provider=provider)
            instance = OpenAICompatibleProvider(
                name=provider,
                base_url=base_url or "https://api.openai.com/v1",
                api_key_env=api_key_env,
                api_key=api_key,
            )
        return await instance.list_models()


__all__ = ["DefaultModelProviderFactory"]
