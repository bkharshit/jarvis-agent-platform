# JARVIS

An open-source, production-grade AI agent platform. Agent runtime first —
tools, MCP, workflows, RAG, and frontend ride on the interfaces the runtime
establishes.

> Status: Phase 1 (agent runtime) complete. See `docs/implementation-plan.md`
> for the build sequence and `docs/adr/` for the design decisions.
> The frontend now ships alongside the backend: the full product shell
> lands first (roadmap F1) and each section enables as its backend
> capability lands — see `docs/architecture/frontend-architecture.md`.

## What it is

A modular monolith (single deployable, clean internal seams):

- **Domain** — pure Pydantic models, no IO: agents, messages, executions,
  events, tools.
- **Ports** — Protocol interfaces for storage, model providers, tools, the
  event sink, and strategies; adapters are swappable.
- **Runtime** — `AgentRuntime` owns the run loop and every limit (iterations,
  token budget, deadline, cancellation). A run *never raises*: it emits
  exactly one terminal event (`run.completed` / `run.failed` /
  `run.cancelled`) and persists the outcome.
- **Event log** — every run appends an ordered event stream: per-run
  `sequence` (gapless, replayable transcripts) and a global BIGSERIAL
  `cursor` that *is* the SSE `Last-Event-ID` (ADR 0003).
- **Persistence** — Postgres from day one (ADR 0002): agents are
  snapshot-versioned and append-only; runs, messages, tool executions, and
  events are typed JSONB rows that reconstruct any run after the fact.
- **Strategies** — `function_calling` (provider-native tool calls) and
  `react` (Thought/Action/Action Input/Final Answer text protocol with
  malformed-action recovery), behind a `StrategyRegistry` port.

## Quickstart

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run jarvis serve          # API on :8000
```

Create an agent and run it. For a no-network smoke, copy the example with
`provider: mock` *and* `strategy.type: function_calling` (the mock's canned
reply never produces a ReAct `Action:`, so a react-strategy agent would run
until its iteration cap and fail gracefully):

```bash
uv run jarvis agent create --file examples/research-agent.yaml
uv run jarvis run research-agent "Explain Kafka consumer groups" --stream
uv run jarvis executions list
uv run jarvis executions show <run-id> --events   # replay the event log
```

The streamed run prints each event (`run.started`, `tool.call.completed`,
…, exactly one terminal event) plus text deltas inline; the replay shows
the same event log from the database.

Health and sanity checks:

```bash
curl -s localhost:8000/healthz
uv run jarvis doctor --ping-model
```

## HTTP API (under `/v1`)

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/agents` | Create agent (publishes v1) — 409 on duplicate name |
| GET | `/agents` | List agents |
| GET/PATCH/DELETE | `/agents/{id}` | Read / update (auto-publishes a version) / delete (409 if runs exist) |
| GET | `/agents/{id}/versions` | Version history (append-only snapshots) |
| POST | `/agents/{id}/run` | Blocking run (queued; waits for the worker) → final `RunResult` |
| POST | `/agents/{id}/stream` | SSE stream of run events |
| GET | `/executions` | List runs (`agent_id`, `status`, `session_id`, `limit`, `offset`) |
| GET | `/executions/{run_id}` | Run + transcript + tool executions |
| POST | `/executions/{run_id}/cancel` | Idempotent cancel — live token in-process, cross-process request row otherwise |
| GET | `/executions/{run_id}/events` | Event replay: JSON, or SSE with `Accept: text/event-stream` |
| GET | `/conversations/{agent_id}/{session_id}/messages` | Conversation history |
| GET | `/capabilities` | Section flags + registry-derived detail the UI renders from |
| GET | `/models` | Live model catalog from a provider endpoint (`provider`, optional `base_url`/`api_key_env`; 502 `model_unreachable`/`model_auth` on failure — ADR 0007) |

Every error has one envelope shape: `{"error": {"kind", "message", "details"}}`.

### Streaming and exactly-once resume

The stream starts before the run does, frames carry
`id: <cursor>` / `event: <type>` / `data: <event>`, and reconnecting with the
last cursor you saw delivers every missed event exactly once — the cursor is
the durable `execution_events.cursor`, so resume survives server restarts:

```bash
curl -N -X POST localhost:8000/v1/agents/{id}/stream \
  -H 'Content-Type: application/json' -d '{"input": "what is 40 + 2?"}'

# ... disconnect after seeing cursor 12, then reconnect:
curl -N -X POST localhost:8000/v1/agents/{id}/stream \
  -H 'Last-Event-ID: 12' -H 'Content-Type: application/json' \
  -d '{"input": "what is 40 + 2?", "run_id": "<run-id>"}'
```

Finished runs replay from the database the same way.

### Queue-backed runs and distributed mode (S1, ADR 0008)

Every API run is enqueued in Postgres and executed by a worker; the API
streams events out of the database, so a run survives the API process.
`jarvis serve` embeds a worker by default, which is why one process behaves
like a self-contained system. For distributed mode — several workers behind
one API, or workers restarted independently:

```bash
JARVIS_EMBEDDED_WORKER=false uv run jarvis serve   # API only (runs queue up)
uv run jarvis worker                               # any number of these
```

Notes:

- A queued run with no worker stays `queued` until one claims it.
- The worker renews a 15s lease while a run executes. If a worker dies, a
  sweeper reaps the lease: a run that emitted nothing is requeued (executed
  again from scratch — safe, it never started); anything else gets exactly
  one terminal `run.failed` (`error_kind="timeout"`, "worker lost (lease
  expired)") or is finished from the terminal event the dead worker already
  wrote. A run is never blindly re-executed.
- Cancelling a queued or foreign-worker run writes a cancel request the
  owning worker's heartbeat pops; a run live in the API's own process gets
  its runtime token directly.

## Agent definitions (YAML)

See `examples/research-agent.yaml` for a full example. Definitions are
versioned: create publishes v1, every update publishes a new immutable
snapshot, and old versions stay byte-for-byte intact — running always pins
one published version.

```yaml
name: my-agent
model: {provider: openai_compatible, model: gpt-4o-mini}
strategy: {type: function_calling}   # or: react
tools:
  - name: calculator
  - name: http_get
    config: {allowed_hosts: [api.example.com]}   # allow-lists are opt-in
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
| `JARVIS_EMBEDDED_WORKER` | `true` | Embed a queue worker in `serve` (ADR 0008; `false` + `jarvis worker` = distributed mode) |
| `JARVIS_WORKER_CONCURRENCY` | `4` | Runs a single worker executes concurrently |
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
role and `CREATE DATABASE jarvis_test`, then
`uv run pytest tests/integration -m db`.

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