"""GET /v1/models — live model catalog from a provider (ADR 0007).

Thin controller: shape query params into a factory call, map ModelError
onto the error envelope. The listing is live IO — deliberately separate
from the registry-derived capabilities payload. Credentials: `api_key_env`
names an environment variable (D18); `credential_id` names a stored BYOK
credential resolved tenant-scoped against the acting principal (S2, ADR
0006) — a foreign or revoked id is 404-shaped via the model_auth envelope
(no existence leak)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import ModelListResponse
from jarvis.domain.agent import EnvCredentialRef, StoredCredentialRef
from jarvis.models.errors import ModelAuthError, ModelConnectionError, ModelError

router = APIRouter(tags=["models"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)


@router.get("/models")
async def list_models(
    container: AppContainer = ContainerDep,
    auth: AuthContext = AuthDep,
    provider: str = Query(..., min_length=1),
    base_url: str | None = Query(None),
    api_key_env: str | None = Query(None),
    credential_id: str | None = Query(None),
) -> ModelListResponse:
    # Exactly one credential source may be named (D18 env-ref or ADR 0006
    # stored-ref); both is a validation error, not a silent precedence.
    if api_key_env and credential_id:
        raise ApiError(422, "validation", "pass either api_key_env or credential_id, not both")
    credential_ref = (
        EnvCredentialRef(type="env", env_var=api_key_env)
        if api_key_env
        else StoredCredentialRef(type="stored", credential_id=credential_id)
        if credential_id
        else None
    )
    # D19 semantics: the environment default fills an absent base_url (mock
    # serves no endpoint, so it reports none).
    effective_base_url = base_url
    if provider != "mock" and effective_base_url is None:
        effective_base_url = container.settings.model_base_url
    try:
        models = await container.models.list_models(
            provider,
            base_url=effective_base_url,
            credential_ref=credential_ref,
            principal=auth.principal,
        )
    except ModelConnectionError as exc:
        raise ApiError(502, "model_unreachable", exc.message) from exc
    except ModelAuthError as exc:
        raise ApiError(502, "model_auth", exc.message) from exc
    except ModelError as exc:
        raise ApiError(422, "model_listing", exc.message) from exc
    return ModelListResponse(provider=provider, base_url=effective_base_url, models=models)


__all__ = ["router"]
