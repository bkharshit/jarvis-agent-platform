"""Strategy plugin discovery (S3, D35) — entry points behind a Settings
allow-list.

Third-party strategies are *packages*: discovery reads
`importlib.metadata.entry_points(group="jarvis.strategies")` once at
container build; an entry point loads **only if its name is in the
allow-list** (`Settings.strategy_plugin_allowlist`). The filesystem is
never scanned and nothing hot-loads — install/uninstall is pip/uv + a
restart.

The degenerate cases are facts the API reports, never boot crashes:

- allow-listed name with no installed entry point → `missing`;
- allow-listed plugin that raises on import → `failed` with its error,
  skipped; the rest still load;
- installed but not allow-listed → skipped silently (opt-in means opt-in).
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as discover_entry_points
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from jarvis.ports.strategy import AgentStrategy

ENTRY_POINT_GROUP = "jarvis.strategies"


class StrategyPluginInfo(BaseModel):
    """One strategy as capabilities reports it. Builtins are mirrored with
    `origin="builtin"` so the listing is uniform; `error` is set only on a
    failed import."""

    name: str
    origin: str = "plugin"  # "plugin" | "builtin"
    distribution: str | None = None
    version: str | None = None
    error: str | None = None


class PluginLoadResult(BaseModel):
    loaded: list[StrategyPluginInfo] = Field(default_factory=list)
    failed: list[StrategyPluginInfo] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    # The loaded instances — plain objects, never serialized, so they live
    # outside the pydantic field surface.
    _instances: dict[str, AgentStrategy] = PrivateAttr(default_factory=dict)

    @property
    def strategies(self) -> dict[str, AgentStrategy]:
        """The loaded strategy instances, keyed by entry-point name — the
        registry's `extra`."""
        return dict(self._instances)


def load_strategy_plugins(
    allowlist: list[str],
    entry_points: Iterable[EntryPoint] | None = None,
) -> PluginLoadResult:
    """Discover and load allow-listed strategy plugins.

    `entry_points=None` runs real `importlib.metadata` discovery; tests pass
    stdlib-constructed `EntryPoint(name, value, group)` objects — `.load()`
    imports `module:attr` and needs no installed distribution.
    """
    if entry_points is None:
        candidates = list(discover_entry_points(group=ENTRY_POINT_GROUP))
    else:
        candidates = [ep for ep in entry_points if ep.group == ENTRY_POINT_GROUP]

    installed: dict[str, EntryPoint] = {}
    for ep in sorted(candidates, key=lambda ep: ep.name):
        installed.setdefault(ep.name, ep)  # duplicates: first wins

    result = PluginLoadResult()
    for name in allowlist:
        found: EntryPoint | None = installed.get(name)
        if found is None:
            result.missing.append(name)
            continue
        info = StrategyPluginInfo(
            name=name,
            distribution=found.dist.name if found.dist is not None else None,
            version=found.dist.version if found.dist is not None else None,
        )
        try:
            strategy = _instantiate(found.load())
        except Exception as exc:  # noqa: BLE001 — recorded, never fatal (D35)
            info.error = f"{type(exc).__name__}: {exc}"
            result.failed.append(info)
        else:
            result.loaded.append(info)
            result._instances[name] = strategy
    return result


def _instantiate(entry: Any) -> AgentStrategy:
    """Entry-point values may be an instance or a zero-arg callable class
    (the same normalization the registry applies to its builtins). The
    structural Protocol is the only check — a bad shape surfaces at the
    runtime's step call site, not here (D5)."""
    strategy: AgentStrategy = entry() if isinstance(entry, type) else entry
    return strategy


__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginLoadResult",
    "StrategyPluginInfo",
    "load_strategy_plugins",
]
