"""GET /v1/models route: live catalog, envelope error mapping (ADR 0007).

The real container is bypassed via dependency override; the provider call
is mocked with respx — no DB, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from jarvis.api.app import create_app
from jarvis.api.deps import get_container
from jarvis.config import Settings

BASE = "http://localhost:9999/v1"


def _stub_container(settings: Settings | None = None) -> SimpleNamespace:
    from jarvis.models.factory import DefaultModelProviderFactory

    # /models only touches `settings`, `models`, and the anonymous Principal;
    # the AuthContext build reads the repo attributes (never used on this
    # route), so they exist as None placeholders.
    return SimpleNamespace(
        # hermetic: the developer's gitignored ./.env now carries
        # JARVIS_AUTH_MODE=required, which would 401 every case here (the
        # S3 lesson — Settings(_env_file=None) everywhere)
        settings=settings or Settings(_env_file=None),
        models=DefaultModelProviderFactory(),
        agents=None,
        executions=None,
        conversations=None,
        auth=None,
    )


@pytest.fixture
def app() -> FastAPI:
    # The real factory for the error-envelope handlers; its lifespan never
    # runs under ASGITransport, so the override supplies the container.
    application = create_app()
    application.dependency_overrides[get_container] = _stub_container
    return application


async def _get(app: FastAPI, params: str) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/v1/models{params}")


class TestModelsRoute:
    @respx.mock
    async def test_live_listing(self, app: FastAPI):
        respx.get(f"{BASE}/models").respond(status_code=200, json={"data": [{"id": "gemma4:31b"}]})
        response = await _get(
            app, f"?provider=openai_compatible&base_url={BASE}&api_key_env=JARVIS_KEY"
        )
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "provider": "openai_compatible",
            "base_url": BASE,
            "models": ["gemma4:31b"],
        }

    @respx.mock
    async def test_absent_base_url_falls_back_to_settings(self, app: FastAPI):
        app.dependency_overrides[get_container] = lambda: _stub_container(
            # auth_mode pinned: init kwargs still lose to an exported
            # JARVIS_AUTH_MODE in the developer's shell otherwise
            Settings(model_base_url=BASE, auth_mode="anonymous")
        )
        route = respx.get(f"{BASE}/models").respond(status_code=200, json={"data": []})
        response = await _get(app, "?provider=openai_compatible")
        assert response.status_code == 200
        assert response.json()["base_url"] == BASE
        assert route.called

    async def test_mock_listing_no_endpoint(self, app: FastAPI):
        response = await _get(app, "?provider=mock")
        assert response.status_code == 200
        body = response.json()
        assert body["models"] == ["mock-small", "mock-large"]
        assert body["base_url"] is None

    @respx.mock
    async def test_unreachable_endpoint_maps_502(self, app: FastAPI):
        respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("refused"))
        response = await _get(app, f"?provider=openai_compatible&base_url={BASE}")
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["kind"] == "model_unreachable"

    @respx.mock
    async def test_auth_failure_maps_502(self, app: FastAPI):
        respx.get(f"{BASE}/models").respond(status_code=401, text="bad key")
        response = await _get(app, f"?provider=openai_compatible&base_url={BASE}")
        assert response.status_code == 502
        assert response.json()["error"]["kind"] == "model_auth"

    async def test_unsupported_provider_maps_422(self, app: FastAPI):
        from jarvis.models.factory import DefaultModelProviderFactory

        container = _stub_container()
        container.models = DefaultModelProviderFactory(extra_providers={"stub": object()})
        app.dependency_overrides[get_container] = lambda: container
        response = await _get(app, "?provider=stub")
        assert response.status_code == 422
        assert response.json()["error"]["kind"] == "model_listing"

    async def test_missing_provider_param_is_422_envelope(self, app: FastAPI):
        response = await _get(app, "")
        assert response.status_code == 422
        assert response.json()["error"]["kind"] == "validation"
