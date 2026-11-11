# JARVIS

**A self-hostable AI agent platform with a production-style architecture —
runtime first.** Agents, tools, MCP, workflows, RAG, evaluations, triggers
and multi-agent delegation all ride on one agent runtime that owns every
limit and never lets a run escape without exactly one persisted terminal
state.

[![CI](https://github.com/bkharshit/jarvis-agent-platform/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/bkharshit/jarvis-agent-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)

> **Status:** every roadmap stage has shipped — F1 (product shell) and
> S1–S14 (S5 was superseded by F1; S9 is a hardening grab-bag). Python /
> FastAPI / Postgres backend, React frontend. See
> [`docs/roadmap.md`](docs/roadmap.md).

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        WEB["React web app<br/>(web/)"]
        CLI["jarvis CLI"]
        HOOK["Webhooks / cron<br/>(triggers)"]
    end

    API["FastAPI transport<br/>routes · SSE · error envelope"]
    Q[("Postgres run queue<br/>(ADR 0008)")]
    W["Worker<br/>(embedded or distributed)"]

    subgraph Core["Agent runtime — owns limits, budget, deadline, cancellation"]
        RT["AgentRuntime"]
        ST["Strategies<br/>function_calling · react · plugins"]
        TR["Tool runtime<br/>builtins · MCP · sub-agents"]
    end

    subgraph Ports["ports/ — Protocol interfaces"]
        direction TB
        MP["Model providers<br/>OpenAI-compatible · mock"]
        DB[("Postgres<br/>typed JSONB · pgvector")]
        EV["Event log + SSE bus<br/>gapless sequence, cursor"]
        SP["Spans<br/>Postgres · OTLP"]
    end

    WEB --> API
    CLI --> RT
    HOOK --> API
    API --> Q --> W --> RT
    RT --> ST --> MP
    RT --> TR
    RT --> EV
    RT --> DB
    RT --> SP
    EV -. "SSE / Last-Event-ID" .-> API
```

`domain/` and `ports/` import only pydantic and the stdlib (lint-enforced,
`mypy --strict`); every adapter depends inward and is swappable.

## What it can do

| Stage | Capability | Design record |
| --- | --- | --- |
| Phase 1 | Agent runtime: snapshot-versioned agents, run loop with iteration / token / deadline / cancel limits, gapless event log, SSE with exactly-once resume, `function_calling` and `react` strategies | [ADR 0001–0005](docs/adr/) |
| F1 | Full product shell in React; sections enable themselves from `GET /v1/capabilities` — nothing unbuilt is faked | [frontend architecture](docs/architecture/frontend-architecture.md) |
| S1 | Queue-backed runs, embedded or distributed workers, Postgres LISTEN/NOTIFY event bus | [ADR 0008](docs/adr/0008-distributed-runs.md) |
| S2 | Auth, multi-tenancy, API keys; BYOK model credentials stored AES-GCM-encrypted and write-only | [ADR 0009](docs/adr/0009-auth-and-tenancy.md), [0006](docs/adr/0006-credential-resolution.md) |
| S3 | Plugin strategies loaded from installed packages behind an allow-list | [decisions D35/D36](docs/decisions.md) |
| S4 | MCP tool servers with credential headers | [ADR 0012](docs/adr/0012-mcp-tool-providers.md), [0013](docs/adr/0013-mcp-credential-headers.md) |
| S6 | Workflow engine with a visual canvas | [ADR 0015](docs/adr/0015-workflow-engine.md) |
| S7 | Run tracing: spans in Postgres, waterfall UI, optional OTLP export | [ADR 0021](docs/adr/0021-otel-tracing.md) |
| S8 | Knowledge bases and RAG on pgvector | [ADR 0020](docs/adr/0020-knowledge-rag.md) |
| S10 | Human-in-the-loop: pause for approval or input, per-call tool decisions, resume | [ADR 0010](docs/adr/0010-human-in-the-loop.md), [0011](docs/adr/0011-per-call-tool-decisions.md) |
| S11 | Evaluation framework: datasets, scorers, LLM-as-judge, run comparison | [ADR 0017](docs/adr/0017-evaluation-framework.md) |
| S12 | Richer memory | [ADR 0016](docs/adr/0016-richer-memory.md) |
| S13 | Triggers: cron schedules, signed webhooks, run-event triggers with a loop guard | [ADR 0019](docs/adr/0019-triggers.md) |
| S14 | Multi-agent: sub-agent delegation and handoffs, one trace across parent and children | [ADR 0018](docs/adr/0018-multi-agent.md) |

## Quickstart (no API key needed)

Needs [uv](https://docs.astral.sh/uv/) and Docker. The `mock` provider
streams a canned reply with zero network, so this runs without any model
credentials:

```bash
uv sync --extra dev
docker compose up -d postgres
uv run jarvis init
uv run jarvis agent create --file examples/mock-agent.yaml
uv run jarvis run mock-agent "Hello, JARVIS" --stream
```

The stream prints each event (`run.started`, `iteration.started`, text
deltas, …) and exactly one terminal event. `uv run jarvis executions list`
shows the persisted run. A mock agent must use
`strategy.type: function_calling` — its canned reply is not ReAct-parseable.

Docker only, no local Python: `docker compose up` builds the API image,
migrates the schema, and serves the API on `:8000` with the mock provider
as the default (CI builds and smoke-tests this stack on every push).

For the web app, start the API and the dev server:

```bash
uv run jarvis serve                          # API on :8000
cd web && npm install && npm run dev         # UI on :5173
```

To use a real model, point an agent at any OpenAI-compatible endpoint
(OpenAI, Ollama, vLLM) — see [`examples/research-agent.yaml`](examples/research-agent.yaml).
Keys are referenced by environment-variable name, never stored in config.

The full HTTP reference is in [`docs/api.md`](docs/api.md); a hands-on tour
of every feature is in [`docs/local-run-guide.md`](docs/local-run-guide.md).

## What it is

A modular monolith (single deployable, clean internal seams):

- **Domain** — pure Pydantic models, no IO: agents, messages, executions,
  events, tools.
- **Ports** — Protocol interfaces for storage, model providers, tools, the
  event sink, and strategies; adapters are swappable.
- **Runtime** — `AgentRuntime` owns the run loop and every limit (iterations,
  token budget, deadline, cancellation). A run *never raises*: it emits
  exactly one terminal event (`run.completed` / `run.failed` /
  `run.cancelled`) and persists the outcome. A run may also *pause* — one
  non-terminal `run.awaiting_input` event — and resume through the same
  queue: a run is a chain of pause/resume segments over one gapless event
  sequence, with one terminal at the end (ADR 0010).
- **Event log** — every run appends an ordered event stream: per-run
  `sequence` (gapless, replayable transcripts) and a global BIGSERIAL
  `cursor` that *is* the SSE `Last-Event-ID` (ADR 0003).
- **Persistence** — Postgres from day one (ADR 0002): agents are
  snapshot-versioned and append-only; runs, messages, tool executions, and
  events are typed JSONB rows that reconstruct any run after the fact.
- **Strategies** — `function_calling` (provider-native tool calls) and
  `react` (Thought/Action/Action Input/Final Answer text protocol with
  malformed-action recovery), behind a `StrategyRegistry` port.


## Agent definitions (YAML)

See `examples/research-agent.yaml` for a full example. Definitions are
versioned: create publishes v1, every update publishes a new immutable
snapshot, and old versions stay byte-for-byte intact — running always pins
one published version.

```yaml
name: my-agent
model: {provider: openai_compatible, model: gpt-4o-mini}
strategy: {type: function_calling}   # or: react, or a plugin strategy
                                     # (S3) — must be on the plugin allow-list
tools:
  - name: calculator
  - name: http_get
    config:
      allowed_hosts: [api.example.com]   # allow-lists are opt-in
      requires_approval: true            # pause for approval (S10)
memory: {enabled: true, max_messages: 20}
```

Builtin tools: `calculator`, `current_time`, `http_get` (host allow-list
enforced). Custom tools subclass `BaseTool` and register on the tool
registry; validation, timeout, and cancellation are handled by the tool
runtime, not the tool.

## Configuration

Environment variables (prefix `JARVIS_`, or a `.env` file). The CLI loads a
local gitignored `.env` from the working directory into the process
environment at startup (real env vars win) — so secret *values* may live in
`.env` locally, while configuration stores only their *names* (ADR 0005):
set `JARVIS_MODEL_API_KEY_ENV=MY_KEY_VAR` in `.env` and `MY_KEY_VAR=...`
alongside it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_DATABASE_URL` | `postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis` | Postgres DSN |
| `JARVIS_MODEL_PROVIDER` | `openai_compatible` | `mock` or `openai_compatible` |
| `JARVIS_MODEL_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (e.g. Ollama) |
| `JARVIS_MODEL_API_KEY_ENV` | `OPENAI_API_KEY` | *Name* of the env var holding the key (ADR 0005) |
| `JARVIS_MODEL_NAME` | `gpt-4o-mini` | Model id |
| `JARVIS_RUN_MAX_ITERATIONS` | `8` | Platform run cap (ADR 0004) |
| `JARVIS_RUN_MAX_TOTAL_TOKENS` | unset | Token budget per run |
| `JARVIS_RUN_TIMEOUT_SECONDS` | unset | Run deadline |
| `JARVIS_AWAITING_INPUT_TIMEOUT_SECONDS` | `86400` | Pause deadline — expired pauses are reaped as `run.cancelled` (S10) |
| `JARVIS_EMBEDDED_WORKER` | `true` | Embed a queue worker in `serve` (ADR 0008; `false` + `jarvis worker` = distributed mode) |
| `JARVIS_WORKER_CONCURRENCY` | `4` | Runs a single worker executes concurrently |
| `JARVIS_AUTH_MODE` | `anonymous` | `anonymous` (fixed default tenant) or `required` (401 without credentials — S2/ADR 0009) |
| `JARVIS_CREDENTIALS_MASTER_KEY` | `JARVIS_CREDENTIALS_MASTER_KEY` | *Name* of the env var holding the base64 32-byte BYOK master key (ADR 0006; unset → 503 `credentials_unavailable`) |
| `JARVIS_LLM_TRACE` | `false` | Debug only: log every model request (messages incl. the system prompt + tool schemas) and response to the backend log, tagged with run id + iteration, and buffer them for the web — the execution detail page shows a per-iteration trace when on. In-memory only (embedded worker), lost on restart — nothing is stored |
| `JARVIS_SPANS_ENABLED` | `true` | Persist run spans in Postgres (S7/ADR 0021) — the kill-switch for the always-on Postgres sink |
| `JARVIS_OTEL_EXPORT_ENABLED` | `false` | Also export spans via OTLP (real OpenTelemetry SDK, one adapter — the runtime imports no `opentelemetry` anything). Export failures are logged and swallowed, never a run outcome |
| `JARVIS_OTEL_ENDPOINT` | `http://localhost:4318/v1/traces` | OTLP/HTTP collector endpoint used when export is enabled |
| `JARVIS_OTEL_SERVICE_NAME` | `jarvis` | `service.name` resource attribute on exported spans |
| `JARVIS_OTEL_HEADERS_ENV` | *(empty)* | Name of an env var holding extra OTLP exporter headers in SDK format (`"api-key=secret,env=prod"` — comma-separated `k=v` pairs) |
| `JARVIS_HOST` / `JARVIS_PORT` | `127.0.0.1` / `8000` | HTTP bind |

## Development

> **Hands-on locally?** `docs/local-run-guide.md` is the copy-pasteable
> walkthrough of every implemented feature (updated per stage).

```bash
make test          # unit suite — no DB, no network, no LLM
make test-db       # docker compose postgres + migrations + integration tests
make lint          # ruff check + format check
make typecheck     # mypy src
```

Integration tests use a dedicated `jarvis_test` database (created and
migrated by the test session itself) and are marked `db` (deselected by
default). Without Docker, run a local Postgres with a `jarvis`/`jarvis`
role (the conftest self-bootstraps `jarvis_test` over
`postgresql://jarvis:jarvis@localhost:5432/jarvis` — no manual
CREATE DATABASE), then:

```bash
uv run pytest tests/integration -m db        # S9: the no-Docker path is the tested one
```

## Layout

```
src/jarvis/
├── domain/        # pure models: agent, message, execution, events, tools
├── ports/         # Protocols: repositories, model client, sink, strategies
├── persistence/   # SQLAlchemy adapters + alembic migrations
├── runtime/       # AgentRuntime (limits, budget, cancellation)
├── strategies/    # function_calling, react
├── prompt/        # prompt engine (template fallback + repair path)
├── models/        # provider factory, openai_compatible, scripted mock
├── tools/         # BaseTool, registry, tool runtime, builtins
├── events/        # in-process sink/bus with cursor assignment
├── api/           # FastAPI transport: routes, schemas, SSE, error envelope
└── cli/           # Typer CLI (jarvis)
docs/adr/          # architecture decision records
```
## Design decisions

The architecture is written down before it is built:

- [`docs/adr/`](docs/adr/) — 21 architecture decision records, from
  "Postgres from day one" to OTel tracing. Contract changes (ports, the
  event envelope, the error shape) need an ADR first.
- [`docs/decisions.md`](docs/decisions.md) — the guiding philosophy and the
  implementation decisions (D1…) made along the way.
- [`docs/architecture/overview.md`](docs/architecture/overview.md) — layering
  and the list of stable interfaces.

## Known limitations and roadmap

JARVIS is a single-developer project, not a hosted product. Review findings
that were deliberately deferred — with their reasoning — are tracked in
[`docs/future-improvements.md`](docs/future-improvements.md); parked ideas
are at the end of [`docs/roadmap.md`](docs/roadmap.md). Notable gaps today:
no hosted deployment, credential master-key rotation loses stored
credentials (KMS re-wrap is the deferred path), and the container
quickstart is exercised in CI rather than on the author's machine.

## How this was built

JARVIS was built with heavy use of an AI coding assistant (Claude Code).
I set the architecture, the stage plan and each stage's acceptance criteria,
reviewed the changes, and ran a manual walkthrough of every stage against
the live system — the scripts are in `docs/walkthrough-*.md`, and the bugs
those sessions found are recorded as "known gotchas" in
[`CLAUDE.md`](CLAUDE.md). Every commit had to pass lint, `mypy`, and the
unit suite.

## License

[MIT](LICENSE).
