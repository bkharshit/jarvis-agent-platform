"""Settings: JARVIS_ env prefix, defaults, extras ignored."""

from jarvis.config import Settings


class TestDefaults:
    def test_database_url_default(self):
        assert Settings(_env_file=None).database_url.startswith("postgresql+asyncpg://")

    def test_model_defaults(self):
        # `_env_file=None` pins the code defaults — a developer's local
        # gitignored `.env` legitimately overrides Settings() otherwise.
        settings = Settings(_env_file=None)
        assert settings.model_provider == "openai_compatible"
        assert settings.model_api_key_env == "OPENAI_API_KEY"

    def test_run_limits_defaults(self):
        settings = Settings(_env_file=None)
        assert settings.run_max_iterations == 8
        assert settings.run_max_total_tokens is None
        assert settings.run_timeout_seconds is None


class TestEnvOverrides:
    def test_prefix_is_jarvis(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql+asyncpg://x/y")
        assert Settings().database_url == "postgresql+asyncpg://x/y"

    def test_unprefixed_var_ignored(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nope/nope")
        assert Settings().database_url != "postgresql+asyncpg://nope/nope"

    def test_unknown_vars_ignored(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPLETELY_UNKNOWN", "whatever")
        assert Settings().model_name  # construction did not raise

    def test_optional_int_from_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_RUN_MAX_TOTAL_TOKENS", "5000")
        assert Settings().run_max_total_tokens == 5000

    def test_bool_coercion_not_needed_here(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PORT", "9001")
        assert Settings().port == 9001


class TestLocalEnvLoading:
    """`_settings()` populates the process env from a local `.env` (ADR 0005:
    config stores names; `.env` is a local way to fill the environment)."""

    def test_dotenv_vars_reach_os_environ(self, tmp_path, monkeypatch):
        from jarvis.cli.main import _settings

        env_file = tmp_path / ".env"
        env_file.write_text("MY_CLI_TEST_SECRET=from-dotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MY_CLI_TEST_SECRET", raising=False)
        try:
            _settings()
            import os

            assert os.environ["MY_CLI_TEST_SECRET"] == "from-dotenv"
        finally:
            import os

            os.environ.pop("MY_CLI_TEST_SECRET", None)

    def test_existing_env_wins_over_dotenv(self, tmp_path, monkeypatch):
        from jarvis.cli.main import _settings

        env_file = tmp_path / ".env"
        env_file.write_text("MY_CLI_TEST_SECRET=from-dotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MY_CLI_TEST_SECRET", "from-process")
        try:
            _settings()
            import os

            assert os.environ["MY_CLI_TEST_SECRET"] == "from-process"
        finally:
            import os

            os.environ.pop("MY_CLI_TEST_SECRET", None)
