"""Strategy plugin discovery (S3, D35) — allow-list gate, failure recording.

Unit tests pass stdlib-constructed EntryPoint objects pointing at modules
that are importable in the dev environment (jarvis itself); the installed
fixture distribution is exercised by the integration suite."""

from __future__ import annotations

from importlib.metadata import EntryPoint

from jarvis.config import Settings
from jarvis.strategies.plugins import load_strategy_plugins

_GROUP = "jarvis.strategies"

_OK = "jarvis.strategies.registry:FunctionCallingStrategy"  # a class -> instantiated
_BROKEN = "jarvis.strategies.registry:NoSuchAttribute"  # AttributeError on load


def _ep(name: str, value: str) -> EntryPoint:
    return EntryPoint(name, value, _GROUP)


def test_allowlisted_plugin_loads_and_instantiates():
    result = load_strategy_plugins(["ok"], [_ep("ok", _OK)])
    assert [info.name for info in result.loaded] == ["ok"]
    assert result.failed == [] and result.missing == []
    # classes are instantiated at load; the registry receives an instance
    from jarvis.strategies.function_calling import FunctionCallingStrategy

    assert isinstance(result.strategies["ok"], FunctionCallingStrategy)


def test_not_allowlisted_is_skipped_silently():
    result = load_strategy_plugins([], [_ep("ok", _OK), _ep("other", _OK)])
    assert result.loaded == [] and result.failed == [] and result.missing == []


def test_allowlisted_name_without_entry_point_is_missing():
    result = load_strategy_plugins(["ghost"], [_ep("ok", _OK)])
    assert result.missing == ["ghost"] and result.loaded == []


def test_import_failure_is_recorded_and_others_still_load():
    result = load_strategy_plugins(["broken", "ok"], [_ep("broken", _BROKEN), _ep("ok", _OK)])
    assert [info.name for info in result.failed] == ["broken"]
    assert result.failed[0].error is not None
    assert "AttributeError" in result.failed[0].error
    assert [info.name for info in result.loaded] == ["ok"]


def test_foreign_group_is_ignored():
    result = load_strategy_plugins(["x"], [EntryPoint("x", _OK, "some.other.group")])
    assert result.missing == ["x"]


def test_duplicate_entry_point_names_first_wins():
    result = load_strategy_plugins(["dup"], [_ep("dup", _OK), _ep("dup", _BROKEN)])
    assert [info.name for info in result.loaded] == ["dup"] and result.failed == []


def test_settings_allowlist_parses_csv_env():
    settings = Settings(strategy_plugin_allowlist="plan_execute, raise_plugin")
    assert settings.strategy_plugin_allowlist == ["plan_execute", "raise_plugin"]
    # a list default stays a list (programmatic construction)
    assert Settings().strategy_plugin_allowlist == []
