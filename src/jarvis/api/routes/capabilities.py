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

from jarvis.api.auth import AuthContext, AuthDep
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
        "enabled": True,
        "summary": "DAG runs reusing the same event model",
    },
    "knowledge": {"enabled": False, "stage": "S8", "summary": "Datasets and retrieval"},
    "evaluations": {"enabled": True, "summary": "Datasets, runs, and scores"},
    "observability": {"enabled": False, "stage": "S7", "summary": "Traces and spans"},
    "plugins": {"enabled": True, "summary": "Strategy plugins and discovery"},
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


def _plugins_detail(container: AppContainer) -> dict[str, Any]:
    """S3 (D35): the strategies listing mirrors the registry (builtins +
    loaded plugins), and the load result reports the degenerate cases —
    failed imports and allow-listed-but-missing names. The allow-list is
    echoed as server config (env), never edited through the API."""
    result = container.strategy_plugins
    return {
        "strategies": [info.model_dump() for info in container.strategies.describe()],
        "failed": [info.model_dump() for info in result.failed],
        "missing": list(result.missing),
        "allowlist": list(container.settings.strategy_plugin_allowlist),
    }


async def _mcp_detail(container: AppContainer, *, tenant_id: str | None) -> dict[str, Any]:
    """S4 (ADR 0012): the MCP section is live — servers listed from the repo
    at request time (counts only, no live connections; the probe route owns
    those). A tenant sees its own rows plus platform-shared ones, exactly
    what its runs would resolve against."""
    servers = await container.mcp_servers.list_servers(tenant_id=tenant_id)
    return {
        "enabled": True,
        "servers": [
            {"id": s.id, "name": s.name, "transport": s.config.type, "enabled": s.enabled}
            for s in servers
        ],
    }


async def _evaluations_detail(container: AppContainer, *, tenant_id: str | None) -> dict[str, Any]:
    """S11 (ADR 0017 §6): the Evaluations section is live — the derived fact
    is the tenant's dataset and eval-run counts, read from the repo at
    request time (the S4 derived-facts pattern)."""
    return {
        "datasets": len(await container.evaluations.list_datasets(tenant_id=tenant_id)),
        "runs": len(await container.evaluations.list_runs(tenant_id=tenant_id)),
    }


async def _workflows_detail(container: AppContainer) -> dict[str, Any]:
    """S6 (ADR 0015 §6): the Workflows section is live — the derived fact is
    the repo's workflow count plus the backend's node-type set (v1)."""
    workflows = await container.workflows.list_workflows(limit=200)
    return {"node_types": ["agent", "tool", "condition"], "count": len(workflows)}


async def build_capabilities(
    container: AppContainer, *, tenant_id: str | None = None
) -> CapabilitiesResponse:
    """Builder — the only IO is the provider capability probe (the models
    factory resolve is async, ADR 0009 §7) and the MCP server listing (S4,
    tenant-scoped); unit tests pass a stub container with async stubs."""
    sections: dict[str, SectionCapability] = {}
    for key, flags in _SECTION_FLAGS.items():
        detail: dict[str, Any] | None = None
        if key == "agents":
            detail = {"strategies": container.strategies.names()}
        elif key == "tools":
            detail = {
                "builtins": [d.model_dump() for d in container.tools.descriptors()],
                "mcp": await _mcp_detail(container, tenant_id=tenant_id),
            }
        elif key == "executions":
            # S10: human-in-the-loop is live — pause frames, the resume
            # route, and the awaiting_input inbox are real (UI enablement).
            # ADR 0014: the debug LLM trace view rides the settings flag.
            detail = {"human_in_the_loop": True, "llm_trace": container.settings.llm_trace}
        elif key == "workflows":
            detail = await _workflows_detail(container)
        elif key == "evaluations":
            detail = await _evaluations_detail(container, tenant_id=tenant_id)
        elif key == "models":
            detail = await _model_providers_detail(container)
        elif key == "plugins":
            detail = _plugins_detail(container)
        elif key == "settings":
            detail = _settings_detail(container)
        sections[key] = SectionCapability(detail=detail, **flags)
    return CapabilitiesResponse(sections=sections)


@router.get("/capabilities")
async def capabilities(
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> CapabilitiesResponse:
    return await build_capabilities(container, tenant_id=auth.principal.tenant_id)


__all__ = ["build_capabilities", "router"]
