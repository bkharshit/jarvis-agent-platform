"""Typed application settings (pydantic-settings, `JARVIS_` env prefix)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- persistence ---------------------------------------------------
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis"

    # --- model layer ----------------------------------------------------
    # `model_api_key_env` names an environment variable holding the API key;
    # the secret itself never enters configuration (ADR 0005).
    model_provider: str = "openai_compatible"
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key_env: str = "OPENAI_API_KEY"
    model_name: str = "gpt-4o-mini"

    # --- auth & tenancy (S2, ADR 0009) -----------------------------------
    # `anonymous` keeps local dev and the CLI friction-free (a fixed default
    # tenant, full access); `required` rejects unauthenticated requests.
    auth_mode: str = "anonymous"  # anonymous | required
    # Name of the env var holding the base64url 32-byte BYOK master key —
    # the *value* never enters configuration (D18 pattern).
    credentials_master_key_env: str = "JARVIS_CREDENTIALS_MASTER_KEY"

    # --- orchestrator limits (ADR 0004: the orchestrator owns limits) ----
    run_max_iterations: int = 8
    run_max_total_tokens: int | None = None
    run_timeout_seconds: float | None = None

    # --- distributed runs (S1, ADR 0008) ----------------------------------
    # Every run goes through the queue. `serve` embeds a worker by default
    # so one process behaves like Phase 1 from the outside; distributed
    # deployments turn this off and run `jarvis worker` separately.
    embedded_worker: bool = True
    worker_concurrency: int = 4

    # --- HTTP server -----------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000


__all__ = ["Settings"]
