"""Capabilities builder contract — no DB, no app (pure builder + stubs)."""

from __future__ import annotations

from types import SimpleNamespace

from jarvis.api.routes.capabilities import _SECTION_FLAGS, build_capabilities
from jarvis.config import Settings
from jarvis.domain.tools import ToolDescriptor
from jarvis.models.capabilities import ModelCapabilities
from jarvis.strategies.plugins import PluginLoadResult, StrategyPluginInfo

EXPECTED_SECTIONS = {
    "agents",
    "executions",
    "conversations",
    "tools",
    "models",
    "workflows",
    "knowledge",
    "evaluations",
    "observability",
    "plugins",
    "triggers",
    "settings",
}


def _stub_container(
    *,
    strategies: list[str],
    builtins: list[ToolDescriptor],
    capabilities: ModelCapabilities | None = None,
    describe: list[StrategyPluginInfo] | None = None,
    plugins: PluginLoadResult | None = None,
    allowlist: list[str] | None = None,
    mcp_servers: list[dict] | None = None,
    llm_trace: bool = False,
    workflows: list = None,  # type: ignore[assignment]
) -> SimpleNamespace:
    caps = capabilities or ModelCapabilities()

    class _StubFactory:
        async def resolve(self, ref: object) -> SimpleNamespace:
            return SimpleNamespace(capabilities=caps)

    async def _list_servers(*, tenant_id: str | None = None) -> list:
        return mcp_servers or []

    async def _list_workflows(*, limit: int = 50, offset: int = 0) -> list:
        return workflows or []

    async def _list_eval_datasets(*, tenant_id: str | None = None) -> list:
        return []

    async def _list_eval_runs(*, tenant_id: str | None = None) -> list:
        return []

    return SimpleNamespace(
        settings=Settings(
            _env_file=None, strategy_plugin_allowlist=allowlist or [], llm_trace=llm_trace
        ),
        tools=SimpleNamespace(descriptors=lambda: builtins),
        strategies=SimpleNamespace(names=lambda: strategies, describe=lambda: describe or []),
        strategy_plugins=plugins or PluginLoadResult(),
        models=_StubFactory(),
        mcp_servers=SimpleNamespace(list_servers=_list_servers),
        workflows=SimpleNamespace(list_workflows=_list_workflows),
        evaluations=SimpleNamespace(list_datasets=_list_eval_datasets, list_runs=_list_eval_runs),
    )


async def test_all_twelve_sections_present() -> None:
    response = await build_capabilities(_stub_container(strategies=["react"], builtins=[]))
    assert set(response.sections) == EXPECTED_SECTIONS


async def test_every_disabled_section_names_its_stage() -> None:
    response = await build_capabilities(_stub_container(strategies=[], builtins=[]))
    for key, section in response.sections.items():
        if not section.enabled:
            assert section.stage, f"disabled section {key!r} must name its stage"
            assert section.summary, f"disabled section {key!r} must carry a summary"


async def test_agents_detail_mirrors_strategy_registry() -> None:
    response = await build_capabilities(
        _stub_container(strategies=["function_calling", "react"], builtins=[])
    )
    detail = response.sections["agents"].detail
    assert detail is not None
    assert detail["strategies"] == ["function_calling", "react"]


async def test_tools_detail_mirrors_tool_registry() -> None:
    descriptor = ToolDescriptor(name="calculator", description="Evaluate arithmetic", parameters={})
    response = await build_capabilities(_stub_container(strategies=[], builtins=[descriptor]))
    detail = response.sections["tools"].detail
    assert detail is not None
    assert detail["builtins"] == [descriptor.model_dump()]
    assert detail["mcp"] == {"enabled": True, "servers": []}


async def test_mcp_detail_lists_servers_from_the_repo() -> None:
    from types import SimpleNamespace as _NS

    servers = [
        _NS(id="s1", name="fixtures", config=_NS(type="stdio"), enabled=True),
        _NS(id="s2", name="weather", config=_NS(type="http"), enabled=False),
    ]
    response = await build_capabilities(
        _stub_container(strategies=[], builtins=[], mcp_servers=servers)
    )
    detail = response.sections["tools"].detail
    assert detail is not None
    assert detail["mcp"] == {
        "enabled": True,
        "servers": [
            {"id": "s1", "name": "fixtures", "transport": "stdio", "enabled": True},
            {"id": "s2", "name": "weather", "transport": "http", "enabled": False},
        ],
    }


async def test_executions_detail_exposes_the_llm_trace_flag() -> None:
    response = await build_capabilities(_stub_container(strategies=[], builtins=[], llm_trace=True))
    detail = response.sections["executions"].detail
    assert detail == {"human_in_the_loop": True, "llm_trace": True}

    off = await build_capabilities(_stub_container(strategies=[], builtins=[]))
    assert off.sections["executions"].detail == {
        "human_in_the_loop": True,
        "llm_trace": False,
    }


async def test_models_detail_mirrors_providers_and_settings() -> None:
    capabilities = ModelCapabilities(
        streaming=True, function_calling=False, structured_output="json_mode"
    )
    response = await build_capabilities(
        _stub_container(strategies=[], builtins=[], capabilities=capabilities)
    )
    detail = response.sections["models"].detail
    assert detail is not None
    providers = {p["name"]: p for p in detail["providers"]}
    assert set(providers) == {"mock", "openai_compatible"}
    assert providers["mock"]["capabilities"] == capabilities.model_dump()
    settings = Settings(_env_file=None)  # hermetic — match the stub container's settings
    assert detail["defaults"] == {
        "provider": settings.model_provider,
        "model": settings.model_name,
        "base_url": settings.model_base_url,
    }
    assert response.sections["models"].mode == "read-only"


def test_section_flags_literal_covers_exactly_the_ia() -> None:
    assert set(_SECTION_FLAGS) == EXPECTED_SECTIONS


async def test_plugins_section_enabled_with_derived_detail() -> None:
    # S3: the section detail is registries-mirror-registries — builtins and
    # loaded plugins in one uniform list, degenerate cases from the load
    # result, allow-list echoed as server config.
    describe = [
        StrategyPluginInfo(name="function_calling", origin="builtin"),
        StrategyPluginInfo(
            name="plan_execute",
            distribution="jarvis-strategy-fixtures",
            version="0.1.0",
        ),
    ]
    plugins = PluginLoadResult(
        failed=[StrategyPluginInfo(name="broken", error="ImportError: boom")],
        missing=["ghost"],
    )
    response = await build_capabilities(
        _stub_container(
            strategies=["function_calling", "plan_execute"],
            builtins=[],
            describe=describe,
            plugins=plugins,
            allowlist=["plan_execute", "ghost", "broken"],
        )
    )
    section = response.sections["plugins"]
    assert section.enabled is True
    assert section.detail is not None
    assert [s["name"] for s in section.detail["strategies"]] == [
        "function_calling",
        "plan_execute",
    ]
    assert section.detail["strategies"][1]["origin"] == "plugin"
    assert section.detail["strategies"][1]["distribution"] == "jarvis-strategy-fixtures"
    assert section.detail["failed"][0]["name"] == "broken"
    assert section.detail["missing"] == ["ghost"]
    assert section.detail["allowlist"] == ["plan_execute", "ghost", "broken"]
