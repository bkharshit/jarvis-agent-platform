"""Credential resolution (S2, ADR 0006): env refs stay lazy (D18), stored
refs are principal-required and tenant-scoped, and the factory maps
CredentialError onto the ModelError taxonomy so a run never raises past D5.
Resolution is async end-to-end (the stored backend reads the DB)."""

from __future__ import annotations

import pytest

from jarvis.domain.agent import EnvCredentialRef, ModelRef, StoredCredentialRef
from jarvis.domain.auth import Principal
from jarvis.models.credentials import DefaultCredentialResolver, EnvCredentialResolver
from jarvis.models.errors import ModelAuthError
from jarvis.models.factory import DefaultModelProviderFactory
from jarvis.ports.credential import CredentialError, ResolvedEnv

_PRINCIPAL = Principal(tenant_id="t1", user_id="u1", mode="session")


class TestEnvCredentialResolver:
    async def test_env_ref_resolves_lazily(self):
        resolved = await EnvCredentialResolver().resolve(
            None, EnvCredentialRef(type="env", env_var="OPENAI_API_KEY")
        )
        assert resolved == ResolvedEnv(type="env", env_var="OPENAI_API_KEY")

    async def test_stored_ref_unavailable(self):
        with pytest.raises(CredentialError):
            await EnvCredentialResolver().resolve(
                _PRINCIPAL, StoredCredentialRef(type="stored", credential_id="c1")
            )


class TestDefaultCredentialResolver:
    async def test_env_ref_without_principal(self):
        resolved = await DefaultCredentialResolver().resolve(
            None, EnvCredentialRef(type="env", env_var="K")
        )
        assert isinstance(resolved, ResolvedEnv)

    async def test_stored_ref_without_backend(self):
        with pytest.raises(CredentialError):
            await DefaultCredentialResolver().resolve(
                _PRINCIPAL, StoredCredentialRef(type="stored", credential_id="c1")
            )

    async def test_stored_ref_requires_principal(self):
        class Stored:
            async def resolve(self, principal, ref):  # pragma: no cover - guards below
                raise AssertionError("must not be reached without a principal")

        with pytest.raises(CredentialError):
            await DefaultCredentialResolver(stored_resolver=Stored()).resolve(
                None, StoredCredentialRef(type="stored", credential_id="c1")
            )

    async def test_stored_ref_delegates_to_backend(self):
        seen = {}

        class Stored:
            async def resolve(self, principal, ref):
                seen["principal"], seen["ref"] = principal, ref
                from jarvis.ports.credential import ResolvedMaterial

                return ResolvedMaterial(type="material", value="sk-live")

        resolved = await DefaultCredentialResolver(stored_resolver=Stored()).resolve(
            _PRINCIPAL, StoredCredentialRef(type="stored", credential_id="c1")
        )
        assert resolved.value == "sk-live"
        assert seen["principal"] == _PRINCIPAL
        assert seen["ref"].credential_id == "c1"


class TestFactoryCredentialThreading:
    def _ref(self, credential_ref) -> ModelRef:
        return ModelRef(
            provider="openai_compatible",
            model="m",
            base_url="http://127.0.0.1:1/v1",
            credential_ref=credential_ref,
        )

    async def test_env_ref_keeps_lazy_env_indirection(self):
        factory = DefaultModelProviderFactory()
        client = await factory.resolve(self._ref(EnvCredentialRef(type="env", env_var="K1")))
        assert client._provider._api_key_env == "K1"
        assert client._provider._api_key is None

    async def test_stored_ref_without_principal_is_auth_error(self):
        factory = DefaultModelProviderFactory()
        with pytest.raises(ModelAuthError):
            await factory.resolve(self._ref(StoredCredentialRef(type="stored", credential_id="c1")))

    async def test_stored_ref_without_stored_backend_is_auth_error(self):
        factory = DefaultModelProviderFactory()
        with pytest.raises(ModelAuthError):
            await factory.resolve(
                self._ref(StoredCredentialRef(type="stored", credential_id="c1")),
                principal=_PRINCIPAL,
            )

    async def test_stored_ref_materializes_through_resolver(self):
        from jarvis.ports.credential import ResolvedMaterial

        class Stored:
            async def resolve(self, principal, ref):
                return ResolvedMaterial(type="material", value="sk-live")

        factory = DefaultModelProviderFactory(
            credential_resolver=DefaultCredentialResolver(stored_resolver=Stored())
        )
        client = await factory.resolve(
            self._ref(StoredCredentialRef(type="stored", credential_id="c1")),
            principal=_PRINCIPAL,
        )
        # Material lives only inside the per-resolve provider instance.
        assert client._provider._api_key == "sk-live"

    async def test_credential_error_maps_to_auth_error(self):
        factory = DefaultModelProviderFactory()  # env-only resolver
        with pytest.raises(ModelAuthError):
            await factory.resolve(
                self._ref(StoredCredentialRef(type="stored", credential_id="c1")),
                principal=_PRINCIPAL,
            )
