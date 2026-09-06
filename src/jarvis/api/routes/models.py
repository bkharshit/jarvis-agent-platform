"""GET /v1/models — live model catalog from a provider (ADR 0007).

Thin controller: shape query params into a factory call, map ModelError
onto the error envelope. The listing is live IO — deliberately separate
from the registry-derived capabilities payload.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import ModelListResponse
from jarvis.domain.agent import EnvCredentialRef
from jarvis.models.errors import ModelAuthError, ModelConnectionError, ModelError

router = APIRouter(tags=["models"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)


@router.get("/models")
async def list_models(
    container: AppContainer = ContainerDep,
    provider: str = Query(..., min_length=1),
    base_url: str | None = Query(None),
    api_key_env: str | None = Query(None),
) -> ModelListResponse:
    # `api_key_env` still names an environment variable (D18); stored-credential
    # listing rides the principal-aware AuthContext in S2's API commit.
    credential_ref = EnvCredentialRef(type="env", env_var=api_key_env) if api_key_env else None
    # D19 semantics: the environment default fills an absent base_url (mock
    # serves no endpoint, so it reports none).
    effective_base_url = base_url
    if provider != "mock" and effective_base_url is None:
        effective_base_url = container.settings.model_base_url
    try:
        models = await container.models.list_models(
            provider, base_url=effective_base_url, credential_ref=credential_ref
        )
    except ModelConnectionError as exc:
        raise ApiError(502, "model_unreachable", exc.message) from exc
    except ModelAuthError as exc:
        raise ApiError(502, "model_auth", exc.message) from exc
    except ModelError as exc:
        raise ApiError(422, "model_listing", exc.message) from exc
    return ModelListResponse(provider=provider, base_url=effective_base_url, models=models)


__all__ = ["router"]
