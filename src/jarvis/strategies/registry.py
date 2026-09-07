"""StrategyRegistry — explicit, keyed by StrategyConfig.type."""

from __future__ import annotations

from jarvis.domain.agent import StrategyConfig
from jarvis.ports.strategy import AgentStrategy
from jarvis.strategies.function_calling import FunctionCallingStrategy
from jarvis.strategies.plugins import StrategyPluginInfo
from jarvis.strategies.react import ReActStrategy

_BUILTINS: dict[str, type[AgentStrategy] | AgentStrategy] = {
    "function_calling": FunctionCallingStrategy,
    "react": ReActStrategy,
}


class UnknownStrategyError(KeyError):
    pass


class DefaultStrategyRegistry:
    """Strategies are stateless, so single shared instances are fine; a
    strategy needing per-run state is constructed fresh per resolve."""

    def __init__(
        self,
        extra: dict[str, AgentStrategy] | None = None,
        plugin_infos: list[StrategyPluginInfo] | None = None,
    ) -> None:
        self._strategies: dict[str, AgentStrategy] = {
            name: (strategy() if isinstance(strategy, type) else strategy)
            for name, strategy in _BUILTINS.items()
        }
        self._strategies.update(extra or {})
        # Capabilities metadata (D35): builtins mirrored, loaded plugins carry
        # their distribution/version. Only *loaded* strategies live here —
        # failed/missing entries are reported by the container's load result.
        self._infos: dict[str, StrategyPluginInfo] = {
            name: StrategyPluginInfo(name=name, origin="builtin") for name in _BUILTINS
        }
        for info in plugin_infos or []:
            self._infos[info.name] = info

    def names(self) -> list[str]:
        """Registered strategy names, sorted — feeds /v1/capabilities."""
        return sorted(self._strategies)

    def describe(self) -> list[StrategyPluginInfo]:
        """Every known strategy with origin/distribution/version, sorted —
        the plugins section detail (D35)."""
        return sorted(self._infos.values(), key=lambda info: info.name)

    def resolve(self, config: StrategyConfig) -> AgentStrategy:
        try:
            return self._strategies[config.type]
        except KeyError:
            raise UnknownStrategyError(
                f"unknown strategy: {config.type!r} (known: {sorted(self._strategies)})"
            ) from None


__all__ = ["DefaultStrategyRegistry", "UnknownStrategyError"]
