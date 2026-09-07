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
) -> SimpleNamespace:
    caps = capabilities or ModelCapabilities()

    class _StubFactory:
        async def resolve(self, ref: object) -> SimpleNamespace:
            return SimpleNamespace(capabilities=caps)

    return SimpleNamespace(
        settings=Settings(strategy_plugin_allowlist=allowlist or []),
        tools=SimpleNamespace(descriptors=lambda: builtins),
        strategies=SimpleNamespace(names=lambda: strategies, describe=lambda: describe or []),
        strategy_plugins=plugins or PluginLoadResult(),
        models=_StubFactory(),
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
    assert detail["mcp"] == {"enabled": False, "stage": "S4"}


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
    settings = Settings()
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
