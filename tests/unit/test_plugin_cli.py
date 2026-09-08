"""`jarvis plugin new` / `jarvis plugin install` (S3 authoring + install UX).

Pure helpers + the command logic, with the installer and entry-point
discovery monkeypatched (no network, no real pip). The scaffold's
generated strategy.py is byte-compiled to prove it is valid Python."""

from __future__ import annotations

import py_compile

from typer.testing import CliRunner

from jarvis.cli import plugins as plugin_cli
from jarvis.cli.main import app

runner = CliRunner()


class _Completed:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""


def _invoke(*args: str):
    return runner.invoke(app, ["plugin", *args])


class TestScaffold:
    def test_new_scaffolds_a_valid_package(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke("new", "my-strategy")
        assert result.exit_code == 0, result.output
        pkg = tmp_path / "my_strategy"
        assert (pkg / "pyproject.toml").is_file()
        assert (pkg / "src/jarvis_my_strategy/__init__.py").is_file()
        strategy = pkg / "src/jarvis_my_strategy/strategy.py"
        assert strategy.is_file()
        py_compile.compile(str(strategy), doraise=True)  # generated code is valid Python
        toml = (pkg / "pyproject.toml").read_text()
        assert 'my_strategy = "jarvis_my_strategy.strategy:MyStrategyStrategy"' in toml
        # entry-point name and module are normalized from the hyphenated name
        assert 'name = "jarvis-my-strategy"' in toml

    def test_new_refuses_an_existing_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _invoke("new", "my-strategy").exit_code == 0
        assert _invoke("new", "my-strategy").exit_code != 0

    def test_new_rejects_a_non_identifier_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke("new", "not a name!")
        assert result.exit_code != 0
        assert not (tmp_path / "not_a_name").exists()

    def test_scaffold_has_no_jarvis_dependency(self, tmp_path, monkeypatch):
        # jarvis is not on PyPI — a scaffold declaring it could never install.
        monkeypatch.chdir(tmp_path)
        _invoke("new", "my-strategy")
        toml = (tmp_path / "my_strategy/pyproject.toml").read_text()
        assert "dependencies" not in toml


class TestAppendAllowlist:
    def test_creates_the_env_file(self, tmp_path):
        added = plugin_cli._append_allowlist(["a"], tmp_path / ".env")
        assert added == ["a"]
        assert (tmp_path / ".env").read_text() == "JARVIS_STRATEGY_PLUGIN_ALLOWLIST=a\n"

    def test_merges_into_an_existing_line_and_preserves_the_rest(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("JARVIS_DATABASE_URL=x\nJARVIS_STRATEGY_PLUGIN_ALLOWLIST=a, b\n")
        added = plugin_cli._append_allowlist(["a", "b", "c"], env)
        assert added == ["c"]
        content = env.read_text()
        assert "JARVIS_DATABASE_URL=x\n" in content
        assert "JARVIS_STRATEGY_PLUGIN_ALLOWLIST=a,b,c\n" in content

    def test_second_call_is_a_noop(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("JARVIS_STRATEGY_PLUGIN_ALLOWLIST=a\n")
        assert plugin_cli._append_allowlist(["a"], env) == []
        assert env.read_text() == "JARVIS_STRATEGY_PLUGIN_ALLOWLIST=a\n"


class TestEntryPointDiscovery:
    def test_names_come_from_the_fresh_subprocess(self, monkeypatch):
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            recorded["snippet"] = cmd[2]
            return _Completed(stdout='["friendly_greeter", "second_one"]\n')

        monkeypatch.setattr(plugin_cli.subprocess, "run", fake_run)
        names = plugin_cli._entry_point_names("Jarvis_Friendly_Greeter")
        assert names == ["friendly_greeter", "second_one"]
        # dist matching normalizes case and underscores
        assert "Jarvis_Friendly_Greeter".lower().replace("_", "-") in recorded["snippet"]
        # the group is the frozen entry-point group
        assert "jarvis.strategies" in recorded["snippet"]

    def test_missing_entry_points_exit_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg/pyproject.toml").write_text('[project]\nname = "some-pkg"\n')
        monkeypatch.setattr(plugin_cli, "_run_installer", lambda *a, **k: None)
        monkeypatch.setattr(plugin_cli, "_entry_point_names", lambda dist: [])
        result = _invoke("install", str(tmp_path / "pkg"))
        assert result.exit_code != 0
        assert "no" in result.output and "entry points" in result.output


class TestInstallCommand:
    def test_install_updates_the_allowlist(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg/pyproject.toml").write_text('[project]\nname = "some-pkg"\n')
        monkeypatch.setattr(plugin_cli, "_run_installer", lambda *a, **k: None)
        monkeypatch.setattr(plugin_cli, "_entry_point_names", lambda dist: ["some_skill"])
        result = _invoke("install", str(tmp_path / "pkg"))
        assert result.exit_code == 0, result.output
        assert "some_skill" in (tmp_path / ".env").read_text()
        assert "restart" in result.output

    def test_install_dedupes_an_already_listed_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg/pyproject.toml").write_text('[project]\nname = "some-pkg"\n')
        (tmp_path / ".env").write_text("JARVIS_STRATEGY_PLUGIN_ALLOWLIST=some_skill\n")
        monkeypatch.setattr(plugin_cli, "_run_installer", lambda *a, **k: None)
        monkeypatch.setattr(plugin_cli, "_entry_point_names", lambda dist: ["some_skill"])
        result = _invoke("install", str(tmp_path / "pkg"))
        assert result.exit_code == 0, result.output
        assert "already allow-listed" in result.output

    def test_local_target_without_pyproject_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "empty-dir").mkdir()
        result = _invoke("install", str(tmp_path / "empty-dir"))
        assert result.exit_code != 0
        assert "pyproject.toml" in result.output
