"""GET /v1/capabilities — the section-flag payload the UI renders from (F1).

The enable/stage flags are a module-level literal: they are a *fact of what
is implemented*, changing only by human decision. The `detail` blocks are
derived from the container at request time — registries mirror registries,
never hardcoded. The payload is a fact of the backend (like the error
envelope), so it can never lie about what exists.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends

from jarvis.api.deps import AppContainer, get_container
from jarvis.api.schemas import CapabilitiesResponse, SectionCapability
from jarvis.domain.agent import ModelRef

router = APIRouter(tags=["capabilities"])

# Module-level Depends singleton (ruff B008): the container is per-app state,
# so every route shares this one dependency declaration.
ContainerDep = Depends(get_container)

# Registered model-provider names → one-line description. The capabilities
# themselves are read from the provider instance (derived, ADR 0005).
_PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "mock": "Scripted provider for tests and demos (no network)",
    "openai_compatible": (
        "Any OpenAI-compatible endpoint via base_url (OpenAI, Ollama, vLLM, LM Studio)"
    ),
}

# One entry per IA section (docs/architecture/frontend-architecture.md).
# Disabled sections always name the roadmap stage that enables them.
_SECTION_FLAGS: dict[str, dict[str, Any]] = {
    "agents": {"enabled": True, "summary": "Create, version, and run agents"},
    "executions": {"enabled": True, "summary": "Browse runs, transcripts, and event replay"},
    "conversations": {"enabled": True, "summary": "Per-session message history"},
    "tools": {"enabled": True, "summary": "Builtin tool registry and agent bindings"},
    "models": {"enabled": True, "mode": "read-only", "summary": "Provider and model info"},
    "workflows": {
        "enabled": False,
        "stage": "S6",
        "summary": "DAG runs reusing the same event model",
    },
    "knowledge": {"enabled": False, "stage": "S8", "summary": "Datasets and retrieval"},
    "evaluations": {"enabled": False, "stage": "S11", "summary": "Datasets, runs, and scores"},
    "observability": {"enabled": False, "stage": "S7", "summary": "Traces and spans"},
    "plugins": {"enabled": False, "stage": "S3", "summary": "Strategy plugins and discovery"},
    "triggers": {"enabled": False, "stage": "S13", "summary": "Cron, webhook, and event rules"},
    "settings": {"enabled": True, "summary": "Auth, tenants, API keys, BYOK credentials"},
}


async def _model_providers_detail(container: AppContainer) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    for name, description in _PROVIDER_DESCRIPTIONS.items():
        client = await container.models.resolve(ModelRef(provider=name, model="capabilities-probe"))
        providers.append(
            {
                "name": name,
                "description": description,
                "capabilities": client.capabilities.model_dump(),
            }
        )
    settings = container.settings
    return {
        "providers": providers,
        "defaults": {
            "provider": settings.model_provider,
            "model": settings.model_name,
            "base_url": settings.model_base_url,
        },
    }


def _settings_detail(container: AppContainer) -> dict[str, Any]:
    """Auth facts and whether BYOK storage is configured — the env-var NAME
    is settings, only presence of the key value is reported (never values)."""
    settings = container.settings
    return {
        "auth_mode": settings.auth_mode,
        "credentials": {
            "available": bool(
                settings.credentials_master_key_env
                and os.environ.get(settings.credentials_master_key_env)
            ),
        },
    }


async def build_capabilities(container: AppContainer) -> CapabilitiesResponse:
    """Builder — the only IO is the provider capability probe (the models
    factory resolve is async, ADR 0009 §7); unit tests pass a stub
    container with an async stub factory."""
    sections: dict[str, SectionCapability] = {}
    for key, flags in _SECTION_FLAGS.items():
        detail: dict[str, Any] | None = None
        if key == "agents":
            detail = {"strategies": container.strategies.names()}
        elif key == "tools":
            detail = {
                "builtins": [d.model_dump() for d in container.tools.descriptors()],
                "mcp": {"enabled": False, "stage": "S4"},
            }
        elif key == "models":
            detail = await _model_providers_detail(container)
        elif key == "settings":
            detail = _settings_detail(container)
        sections[key] = SectionCapability(detail=detail, **flags)
    return CapabilitiesResponse(sections=sections)


@router.get("/capabilities")
async def capabilities(
    container: AppContainer = ContainerDep,
) -> CapabilitiesResponse:
    return await build_capabilities(container)


__all__ = ["build_capabilities", "router"]
