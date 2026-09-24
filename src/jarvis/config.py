"""Typed application settings (pydantic-settings, `JARVIS_` env prefix)."""

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # How long a paused (awaiting_input) run may sit before the sweeper's
    # pause-reaper cancels it (S10, ADR 0010 §6) — no run is ever stuck.
    awaiting_input_timeout_seconds: float = 86_400.0
    # S14 (ADR 0018 §5, D55): how deep sub-agent delegation may nest — a
    # root run is depth 0, its children depth 1, so the default allows
    # supervisor → worker → worker and refuses great-grandchildren.
    sub_agent_max_depth: int = 2
    # S14 (ADR 0018 §6, D56): the handoff transfer renders the predecessor's
    # transcript as text, truncated from the START to this many characters —
    # the recent context is what a successor needs.
    handoff_max_transcript_chars: int = 20_000

    # --- triggers (S13, ADR 0019) ------------------------------------------
    # The scheduler loop is serve-side only (independent of embedded_worker —
    # it only enqueues; workers anywhere execute); `jarvis worker` never runs
    # it. The guarded claim tolerates overlap if that ever changes.
    trigger_scheduler_enabled: bool = True
    trigger_poll_seconds: float = 5.0
    # The event-trigger chain clamp (§6): a manual run's outcome may fire
    # triggers; a trigger-fired run's outcome may not fire another.
    trigger_max_depth: int = 1
    # Inbound webhook bodies larger than this are refused 413 (§4).
    trigger_webhook_max_body_bytes: int = 65_536
    # The `{{output}}` context var on event triggers is capped to this many
    # characters before rendering (§2).
    trigger_event_max_output_chars: int = 8_000

    # --- knowledge / RAG (S8, ADR 0020) -----------------------------------
    # Caps and defaults ONLY — the embedding model itself rides each
    # dataset's immutable config, nothing platform-level pins it.
    knowledge_chunk_chars: int = 1_200
    knowledge_overlap_chars: int = 150
    # A document larger than this is refused 422 at the ingest boundary.
    knowledge_max_doc_chars: int = 200_000
    knowledge_default_top_k: int = 4
    # Batched embedding: texts per /embeddings request during ingest.
    knowledge_embed_batch: int = 32
    # The embed call's HTTP budget (ingest + query — both seconds-scale).
    knowledge_embed_timeout: float = 30.0

    # --- observability (S7, ADR 0021) -------------------------------------
    # Postgres span persistence is ALWAYS on (D72) — this is the kill-switch.
    spans_enabled: bool = True
    # OTLP export is additive and OFF by default (D75): the same finished
    # spans also go to a collector over the SDK's OTLP/HTTP exporter.
    otel_export_enabled: bool = False
    otel_endpoint: str = "http://localhost:4318/v1/traces"
    otel_service_name: str = "jarvis"
    # Name of the env var holding the exporter's headers, SDK format
    # ("api-key=secret,env=prod"); the values never enter config (D18).
    otel_headers_env: str = ""

    # --- MCP tool providers (S4, ADR 0012) --------------------------------
    # Per-server connect + tools/list budget. Per-CALL timeouts ride binding
    # config through the ToolRuntime envelope — this is the one new knob.
    mcp_connect_timeout: float = 15.0

    # --- distributed runs (S1, ADR 0008) ----------------------------------
    # Every run goes through the queue. `serve` embeds a worker by default
    # so one process behaves like Phase 1 from the outside; distributed
    # deployments turn this off and run `jarvis worker` separately.
    embedded_worker: bool = True
    worker_concurrency: int = 4

    # --- strategy plugins (S3, D35) ---------------------------------------
    # Names of installed `jarvis.strategies` entry points that may load.
    # Empty default — nothing loads unless opted in. The env form is a
    # comma-separated string (pydantic-settings has no list fields from env):
    #   JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute,raise_plugin
    # NoDecode: the CSV string must NOT hit pydantic-settings' implicit JSON
    # decode for complex fields (it would SettingsError before the validator).
    strategy_plugin_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("strategy_plugin_allowlist", mode="before")
    @classmethod
    def _csv_to_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # --- debug ------------------------------------------------------------
    # Print-and-forget LLM trace: log every model request/response (the
    # actual messages incl. the system prompt + tool schemas) to the backend
    # log. Nothing is stored — no DB, no API, no events.
    llm_trace: bool = False

    # --- HTTP server -----------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000


__all__ = ["Settings"]
