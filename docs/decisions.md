# JARVIS — Decision Log

> Every major decision taken so far, in one place. Decisions formalized as
> ADRs are summarized here with a pointer to the full record in `docs/adr/`;
> decisions made during implementation (not worth a formal ADR, but worth
> recording) are listed in §3. New stages should append to this log or write
> an ADR — see the ground rule in `docs/roadmap.md`.

## 1. Philosophy & architecture

| # | Decision | Rationale |
|---|---|---|
| 1.1 | **Agent-runtime-first**: tools platform, MCP, workflows, RAG, frontend, plugins, multi-agent all ride on interfaces the runtime establishes now. | Dify paid a large refactoring cost extracting its runtime late (`dify-agent` service, `graphon` package); clean seams up front avoid that tax. |
| 1.2 | **Modular monolith, no premature microservices.** One deployable; queue-backed sinks are a future transport swap, not a rewrite. | Phase 1 ships one process; the seams (`EventSink`, `EventStream`, `ExecutionContext`) already assume distribution. |
| 1.3 | **Pure-Pydantic domain + Protocol ports, dependency rule lint-enforced.** `domain/` and `ports/` import only pydantic/stdlib; everything else depends inward; `mypy --strict` on domain/ports. | Adapters are swappable by construction — CLI and HTTP already share `AppContainer` and nothing else changed to support both. |
| 1.4 | **Do not over-engineer Phase 1.** No plugins/marketplace, multi-tenancy, RAG, workflows, multi-agent, Redis. | Every deferral names its seam in `docs/roadmap.md`; nothing Phase 1 built needs rewriting for them. |
| 1.5 | **Fully testable with no DB, no network, no LLM.** `MockModelProvider` (scripted turns, failure injection, request recording) + unit suite of 150 tests; integration suite (27 tests) isolated behind the `db` marker. | The runtime is verifiable in seconds from Python; LLM/DB paths are exercised deliberately, not accidentally. |

## 2. Formal ADRs (0001–0005)

| ADR | Decision (one line) | Key consequences |
|---|---|---|
| [0001](adr/0001-postgres-day-one.md) | **PostgreSQL from day one** — no SQLite path, no Mongo; JSONB payloads; durable event log replaces Redis streams. | Dev/test need Postgres (compose or local); alembic from the first commit; up/down tested. |
| [0002](adr/0002-typed-jsonb-persistence.md) | **Typed Pydantic-over-JSONB persistence** — every persisted payload is a Pydantic model in a JSONB column; scalars stay real columns; the model is the single schema authority. | Schema drift fails loudly (`extra="forbid"`); payloads queryable (GIN); serialization boundary lives at the repository edge. |
| [0003](adr/0003-event-envelope-and-terminal-semantics.md) | **One event envelope + exactly-one-terminal semantics** — `{event_id, run_id, sequence, created_at, type}`, per-run gapless sink-assigned `sequence`; exactly one terminal event per run; the global `execution_events.cursor` BIGSERIAL doubles as the SSE `Last-Event-ID`. | Any run is reconstructible by sequence replay; only the orchestrator may `finalize()`; queue-backed sinks keep the same contract. |
| [0004](adr/0004-orchestrator-owns-limits.md) | **Orchestrator owns limits; a strategy owns one step** — `strategy.step()` performs at most one model invocation and returns `ToolCallsStep`/`FinishStep`; `AgentRuntime.run()` owns loop, caps, budget, deadline, cancellation, persistence, and terminal emission. | Runaway strategies are structurally impossible; failure classification is centralized; blocking `/run` and `/stream` call the same method (identical event sequences, test-guarded). |
| [0005](adr/0005-single-openai-compatible-adapter.md) | **One OpenAI-compatible adapter via `base_url` injection** (httpx, no SDKs) + scripted mock provider; `generate()`/`stream()` separate methods; typed error taxonomy with narrow retry (RateLimit + Connection only); capabilities declared per provider. | Serves OpenAI/Ollama/vLLM/LM Studio with one adapter; Ollama's json-mode-only structured output → schema-in-prompt fallback; secrets never enter config (env-var *name* indirection). |

## 3. Implementation decisions (Phase 1 build)

Date-stamped; each with the reasoning that drove it and where it lives in
the code.

### Persistence & versioning

- **D1 (2026-09-03) — Agent versions are append-only snapshots, not references.**
  `agent_versions.snapshot` is the complete definition at publish time;
  creating publishes v1, every update publishes a new immutable version, old
  versions stay byte-identical, and every run pins a version id. Replaying a
  historical run needs only the snapshot — no reconstruction from diffs.
  *(persistence/`AgentVersionRow`, repos `update_and_publish`)*
- **D2 (2026-09-04) — The runtime writes a RUNNING row at run start.** The
  original design persisted the row only at finalize, so live runs were
  invisible to `/executions` list filters and `cancel` 404'd on them. The
  fix writes the RUNNING row from the run's first event. *(agent_runtime.py;
  commit f9d2595)*
- **D3 (2026-09-04) — Stream resume prefers the live in-process sink before
  the DB row.** A RUNNING row may not be readable yet at resume time; the
  live sink is authoritative for live runs, DB replay (`replay_with_cursor`)
  for finished ones. Same commit.
- **D4 (2026-09-04) — Migration enums are created by `op.create_table`, never
  explicitly.** SQLAlchemy 2.0.52 has no `create_type` kwarg (silently
  ignored); a checkfirst pre-create plus the table's own CREATE TYPE fails
  with `DuplicateObjectError`. Downgrades keep explicit
  `.drop(checkfirst=True)`. *(migrations/0001)*

### Runtime semantics

- **D5 — A run never raises.** Every failure mode (limits, deadline, model
  error, tool error, cancellation) resolves to a persisted terminal state:
  `succeeded | failed | cancelled | timed_out`, with `error_kind`
  classification (max_iterations | timeout | model | tool | output_schema).
  The API never needs a try/except around the runtime.
- **D6 — Cancellation is cooperative, not a task kill.** `ctx.cancel` is a
  token checked at defined checkpoints (loop top, pre-tool-execution); a
  sleeping tool finishes, then the run ends at the next checkpoint. This
  keeps tool code free of asyncio-cancellation hazards and makes
  cross-process cancellation (roadmap S1) a token-write instead of a signal.
- **D7 — The orchestrator double-clamps iterations.** The per-agent
  `max_iterations` (1–32) is enforced at the loop bottom *and* the platform
  `RunLimits` budget is checked at the top of every iteration — a hostile
  agent definition cannot escape the platform cap (ADR 0004's structural
  guarantee).
- **D8 (2026-09-04) — ReAct strips the `Final Answer:` marker from the finish
  message.** The user-facing `final_message` is the answer, not the protocol
  line; the raw text is preserved in the assistant message history.
- **D9 — Malformed ReAct actions recover, never crash.** Missing `Action:`,
  unknown tool, or unparseable input produces a developer-message correction
  on the next iteration instead of failing the run. *(strategies/react.py)*

### Tools

- **D10 — Tools implement `_execute` only; everything else is the runtime's
  job.** Validation against JSON Schema, per-tool timeout, cancellation
  checks, output truncation, and exception→error-result live in
  `ToolRuntime` (template-method pattern). A broken tool returns an error
  result; it cannot crash a run. *(tools/runtime.py)*
- **D11 — http_get allow-lists are opt-in.** An `http_get` binding without
  `allowed_hosts` refuses every request; `*` is allowed but documented as
  tests-only. Fail-closed by design. *(tools/builtin/http_get.py)*
- **D12 — Parallel tool calls deferred.** `ModelCapabilities.parallel_tool_calls`
  exists (default False); the runtime executes calls sequentially in Phase 1.
  *(roadmap S9)*

### API & transport

- **D13 — Every error has one envelope:** `{"error": {kind, message,
  details}}` — ApiError, FastAPI validation errors, and unexpected 500s all
  map through it; `missing required field` 422s carry the standard
  `details.errors` shape. *(api/errors.py, app.py)*
- **D14 — Transport parity is a contract.** Blocking `/run` and streaming
  `/stream` execute the same `AgentRuntime.run()`; the stream subscribes
  *before* the run task starts so no event is missed.
- **D15 — SSE `Last-Event-ID` is the durable DB cursor.** Exactly-once
  resume for finished runs works across server restarts because the cursor
  is `execution_events.cursor` BIGSERIAL, not in-memory state. Live-run
  resume uses the live sink (D3); both serve the same cursor space.
- **D16 (2026-09-04) — `/executions/{id}/events` returns JSON by default and
  SSE on `Accept: text/event-stream`** — same cursor space either way.
  `response_model=None` because the return type is genuinely polymorphic.
- **D17 — `ToolResult.output` is top-level in API responses** (not nested
  under a `result` key) because the repo returns domain `ToolResult`
  objects; the API serializes the domain model directly.

### Model layer & configuration

- **D18 — Secrets are referenced, never stored.** `api_key_env` names an
  environment variable (ADR 0005); `Settings.model_api_key_env` follows the
  same pattern; per-agent `ModelRef` carries `base_url`/`api_key_env`
  overrides.
- **D19 — `JARVIS_MODEL_PROVIDER` is a fallback default, not an override.**
  An agent definition pins its own `model.provider`; the env var only fills
  provider-less defaults. (Discovered in CLI smoke — mock smoke needs a YAML
  variant.)
- **D20 (2026-09-03) — `pyyaml` moved to main dependencies.** The CLI's
  `agent create --file` needs it at runtime, not just for dev.

### Process & tooling

- **D21 — Gates at every commit:** `pytest tests/unit` (150), `ruff check
  src tests`, `mypy src` — all green before a commit lands; one commit per
  plan sequence step.
- **D22 (2026-09-04) — Integration tests self-bootstrap.** A dedicated
  `jarvis_test` database is created, migrated (up/down/up), and truncated
  per-test by the session fixture; alembic commands run in sync context
  because the migration's `env.py` drives its own event loop. `db`-marked
  tests are deselected by default.
- **D23 (2026-09-04) — CLI surfaces unique-constraint violations as friendly
  conflicts**, not raw tracebacks (duplicate agent names being the common
  case). *(cli/main.py `_with_container`)*
- **D24 — No Docker is not a blocker.** This machine runs local Postgres
  instead of compose; `make test-db` still targets Docker (documented
  limitation), and `pytest tests/integration -m db` works against any
  reachable Postgres.

## 4. Explicit deferrals (decided *not* to build in Phase 1)

Redis/queues · plugins & marketplace · multi-tenancy/auth · RAG · workflow
engine · multi-agent · human-in-the-loop · richer memory · evaluation ·
triggers · frontend · MCP · parallel tool calls · jinja2 sandboxing · OTel
exporters. Each deferral names its seam in `docs/roadmap.md` (stages
S1–S14) — the deferral is a sequencing decision, not an architectural
rejection.