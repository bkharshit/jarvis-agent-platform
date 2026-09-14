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
| 1.6 (2026-09-04) | **The frontend ships alongside the backend, not after it.** The full product IA (Agents, Workflows, Tools, MCP, Models, Knowledge, Executions, Evaluations, Observability, Plugins, Triggers, Settings — Dify-inspired) is designed up front and ships as the app shell against the Phase 1 API; every remaining section is gated on real backend capability (its roadmap stage), rendered disabled/coming-soon, never faked. Supersedes roadmap stage S5; see F1 in `docs/roadmap.md` and `docs/architecture/frontend-architecture.md`. | IA/routing decisions are cheapest now and most expensive to retrofit; an early shell gives continuous end-to-end visibility and forces API-design feedback early. The capabilities payload (`GET /v1/capabilities`) keeps enablement a backend *fact*, not a frontend promise. Backend stages keep their gates; each stage's last item is enabling its UI section. |

## 2. Formal ADRs (0001–0008)

| ADR | Decision (one line) | Key consequences |
|---|---|---|
| [0001](adr/0001-postgres-day-one.md) | **PostgreSQL from day one** — no SQLite path, no Mongo; JSONB payloads; durable event log replaces Redis streams. | Dev/test need Postgres (compose or local); alembic from the first commit; up/down tested. |
| [0002](adr/0002-typed-jsonb-persistence.md) | **Typed Pydantic-over-JSONB persistence** — every persisted payload is a Pydantic model in a JSONB column; scalars stay real columns; the model is the single schema authority. | Schema drift fails loudly (`extra="forbid"`); payloads queryable (GIN); serialization boundary lives at the repository edge. |
| [0003](adr/0003-event-envelope-and-terminal-semantics.md) | **One event envelope + exactly-one-terminal semantics** — `{event_id, run_id, sequence, created_at, type}`, per-run gapless sink-assigned `sequence`; exactly one terminal event per run; the global `execution_events.cursor` BIGSERIAL doubles as the SSE `Last-Event-ID`. | Any run is reconstructible by sequence replay; only the orchestrator may `finalize()`; queue-backed sinks keep the same contract. |
| [0004](adr/0004-orchestrator-owns-limits.md) | **Orchestrator owns limits; a strategy owns one step** — `strategy.step()` performs at most one model invocation and returns `ToolCallsStep`/`FinishStep`; `AgentRuntime.run()` owns loop, caps, budget, deadline, cancellation, persistence, and terminal emission. | Runaway strategies are structurally impossible; failure classification is centralized; blocking `/run` and `/stream` call the same method (identical event sequences, test-guarded). |
| [0005](adr/0005-single-openai-compatible-adapter.md) | **One OpenAI-compatible adapter via `base_url` injection** (httpx, no SDKs) + scripted mock provider; `generate()`/`stream()` separate methods; typed error taxonomy with narrow retry (RateLimit + Connection only); capabilities declared per provider. | Serves OpenAI/Ollama/vLLM/LM Studio with one adapter; Ollama's json-mode-only structured output → schema-in-prompt fallback; secrets never enter config (env-var *name* indirection). |
| [0006](adr/0006-credential-resolution.md) | **Credential resolution: references with optional encrypted storage** (2026-09-05, decision-only — implementation deferred to S2). Agents hold credential *references*, never material; a `CredentialResolver` port owns materialization (env resolver for self-hosted, stored/encrypted resolver for hosted BYOK); credentials are write-only through the API; resolution is tenant-scoped; exact crypto/KMS details deferred to S2. | `api_key_env` unchanged until S2 (no user-facing capability before the tenant model exists); D18 amended with a pointer, not overridden; S2 gains the BYOK work item (resolver port, stored credentials, `credential_ref` migration, tenant scoping, secret-free API responses). |
| [0007](adr/0007-live-model-listing.md) | **Live model listing** — `list_models` on the provider/factory seam; `GET /v1/models`; agent editor suggests the endpoint's real catalog in a datalist, free text always valid. (2026-09-05) | Registries/capabilities stay registry-derived — live IO never enters them; the catalog is what the endpoint *answered*, not a configured fact. |
| [0008](adr/0008-distributed-runs.md) | **Distributed runs** — every run is queue-backed (Postgres `SKIP LOCKED` + leases); runs survive the API process; `PgEventStream` (LISTEN/NOTIFY wake-ups over the DB tail) replaces in-route sinks; cross-process cancellation via a `run_cancels` request row the owning worker's heartbeat pops. (2026-09-05) | No dual run-mode: `serve` embeds a worker by default, distributed mode is `JARVIS_EMBEDDED_WORKER=false` + `jarvis worker` processes; the sweeper reaps expired leases (requeue if 0 events, exactly one terminal failure otherwise); `EventStream.subscribe` yields `(cursor, event)` pairs — the one amendment to a frozen port, recorded in the ADR itself. |

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
  overrides. *(Amended 2026-09-05: ADR 0006 records the decision-only
  future direction — S2 adds optional encrypted BYOK credential storage
  behind a `CredentialResolver` port, with tenant-scoped authorization and
  write-only APIs. Everything an agent persists stays a reference, never
  material. Current behavior is unchanged until S2.)*
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
- **D25 (2026-09-05) — Live model listing (ADR 0007).** `ModelProvider`
  and `ModelProviderFactory` gain `list_models`; `GET /v1/models` exposes
  it; the agent editor's Model field becomes a datalist-backed combo box
  over the endpoint's real catalog, with free text always valid. The
  capabilities payload stays registry-derived — live IO never enters it.
- **D26 (2026-09-05) — Queue-always with an embedded worker (S1, ADR 0008).**
  No dual run-mode: every API run is enqueued and executed by a `Worker`
  through the same `AgentRuntime`. `serve` embeds a worker by default
  (`JARVIS_EMBEDDED_WORKER=true`, `JARVIS_WORKER_CONCURRENCY=4`) so one
  process behaves exactly like Phase 1 from the outside; distributed mode
  is `JARVIS_EMBEDDED_WORKER=false` plus any number of `jarvis worker`
  processes. Routes never touch a local sink — they read the run through
  `PgEventStream` (the DB is the sole source of truth), so streams and
  blocking runs survive the API process. The CLI still drives the runtime
  in-process directly (it is a runtime consumer like the tests, not a
  transport); API clients always queue.

  *Load profile on Postgres (the accepted trade, measured against the
  alternative of a broker):* claim polling is one indexed
  `SKIP LOCKED` query per 0.5s per worker; the heartbeat is one lease
  UPDATE + one cancel-pop SELECT per 2s per executing run; the sweeper
  one SELECT per 30s per worker; `NOTIFY` is fire-and-forget per event.
  Steady-state queue load is O(workers + concurrent runs) at a few simple
  row ops per second each. Event appends and message persistence predate
  S1 (ADR 0002) — the genuinely new load is the subscriber tail (replay
  per batch, 1s fallback poll only when no notify arrived) and is bounded
  by the number of live subscribers. The expected scaling ceiling is
  event write throughput (`text.delta` amplification), addressed by sink
  coalescing and `execution_events` partitioning/retention — not the
  queue. *Scale-out plan:* workers scale horizontally today (`SKIP
  LOCKED` claims need no coordination); when measured load demands it,
  `SqlRunQueue` is swapped for a broker adapter behind the same
  `RunQueue` port — wake-ups/claims move, run state and event
  durability stay in Postgres, so the recovery semantics proven in the
  S1 walkthrough (`docs/walkthrough-s1.md`) are preserved either way.
- **D27 (2026-09-05) — The sweeper reports worker loss as
  `error_kind="timeout"`** — `RunFailed.error_kind` is a frozen Literal
  (`max_iterations|timeout|model|tool|output_schema`, ADR 0003); widening
  it needs an ADR, and "the run died without finishing" is genuinely a
  timeout-shaped outcome (it did not finish within its claim). The error
  string carries the specifics: `"worker lost (lease expired) — run did
  not finish within its claim"`. A new kind (`worker_lost`) would be
  preferable at the next envelope-revisiting ADR.

- **D28 (2026-09-06) — The credential-resolution seam is async (S2).**
  Stored BYOK credentials resolve through the database, so resolution is IO
  and the seam must await: `CredentialResolver.resolve`,
  `ModelProviderFactory.resolve` (and `list_models`), `StoredResolver`,
  `EnvCredentialResolver`, and `DefaultCredentialResolver.resolve` are all
  `async def`. Alternatives rejected: a second sync engine/driver just for
  the resolver (two connection pools, a driver dependency, and a lie about
  what the operation is), and preloading credentials outside the runtime.
  The ADR 0009 consequences amendment pre-declared the signature change
  (CLAUDE.md rule 7). One follow-on rule this forced, now locked by unit
  tests: **model resolution happens inside `AgentRuntime.run`'s try block**
  — a resolution failure is a persisted terminal `model` failure (D5). An
  escape past the runtime makes the worker treat the message as a claim
  failure and retry it forever, with no terminal state (found live in the
  S2 isolation e2e).
- **D29 (2026-09-06) — Tenant boundaries read as 404, secrets are
  write-only.** Cross-tenant ids (agents, executions, members, API keys,
  credentials) resolve to 404 `not_found`, never 403 — no existence leak
  (ADR 0009 §4); scoping happens at the repository boundary as WHERE
  clauses, never post-filters. BYOK material is write-only through the API:
  the secret enters on create/update and no response, snapshot, or log ever
  carries plaintext or the ciphertext envelope (locked by integration
  tests). Member deletes are refused while the user owns API keys or
  credentials even if revoked — the audit rows reference the user, so the
  user row survives.
- **D30 (2026-09-06) — BYOK encryption is AES-GCM with a local master key;
  KMS is deferred.** `JARVIS_CREDENTIALS_MASTER_KEY` names an env var
  holding a base64 32-byte key (ADR 0005 pattern: config stores the
  *name*, never the value). Envelope: `{v, key_id, nonce, ct}`. Tamper
  fails loudly (decrypt error → persisted `model` failure; missing key →
  503 `credentials_unavailable`, capability reports unavailable — it never
  silently degrades). Envelope-encryption via a cloud KMS is the natural
  swap at the same seam (see ADR 0006) and is deliberately not built yet.
- **D31 (2026-09-06) — A pause is a segment, not a terminal (S10).** A run
  pauses via one new non-terminal event `run.awaiting_input` (ADR 0010) and
  a new non-terminal status `awaiting_input`; `is_terminal`,
  `TERMINAL_EVENT_TYPES`, and exactly-one-terminal are unchanged — a
  paused run simply has no terminal event *yet*, and the gapless per-run
  sequence continues across segments. Terminal-adjacency is the new
  invariant: no non-terminal event after `run.awaiting_input` within a
  segment. Pause is a runtime decision (ADR 0004): the loop returns
  without `finalize()`, the worker acks the queue row (nothing holds a
  paused run — cancel/reap act directly on the row).
- **D32 (2026-09-06) — Resume is one more queue segment, and limits span
  the chain (S10).** `POST /executions/{id}/resume` upsert-merges a
  `ResumeRequest` into the run's existing queue payload (the principal
  and deadline live only there, ADR 0008) and flips the row to pending —
  any worker claims it. The resumed segment rebuilds messages from the
  persisted transcript (system prompt + run messages + the resume
  append), seeds usage from the run row and iterations from persisted
  `iteration.started` events, and keeps the original deadline: `max_iterations`
  and the token budget bound the whole chain, not a segment (ADR 0004). A
  crashed resume segment never requeues — a resumed run always has prior
  events, so S1's expired-lease policy lands on terminal failure, never a
  blind re-execution the gapless sequence could not survive.
- **D33 (2026-09-07) — Tool approval is per-call; silence is denial
  (S10 follow-on, ADR 0011).** The resume body gains a third variant,
  `{"decisions": {"<call_id>": bool}}`, alongside the frozen `content` and
  `tool_approval` (batch shorthand, kept). The runtime was already
  per-call — `refusals: set[str]` closes each declined call with a refusal
  tool message while the rest of the batch executes — so only the contract
  changed. Default-deny is the rule: a pending call absent from the map is
  declined, never run; unknown ids are ignored (the pause event's
  `pending_calls` is the authority). Found as a live UI bug: the pause
  card rendered per-call Approve/Reject buttons that all posted the same
  batch boolean.
- **D34 (2026-09-07) — A refusal is an event (ADR 0011 §3).** A declined
  call emits `tool.call.declined` next to its refusal tool message. ADR
  0010's "no events — it never ran" was right about *execution* but left
  the human decision invisible: replay showed the call stuck at
  `requested` forever, indistinguishable from a cancelled run. The event
  records the decision, not an execution — no started/completed, no
  `tool_executions` row — and the store folds it into a distinct
  `declined` card status so live ≡ replay.

- **D35 (2026-09-07) — Plugins load from installed entry-points behind a
  Settings allow-list (S3).** Third-party strategies are *packages*:
  discovery reads `importlib.metadata.entry_points(group="jarvis.strategies")`
  once at container build; an entry point loads **only if its name is in
  `Settings.strategy_plugin_allowlist`** (env `JARVIS_STRATEGY_PLUGIN_ALLOWLIST`,
  comma-separated; empty default — nothing loads unless opted in). The
  filesystem is never scanned; there is no hot load — install/uninstall is
  `pip|uv install` + restart. Three degenerate cases are facts the API
  reports, never boot crashes: an allow-listed name with no installed entry
  point is recorded as `missing`; an allow-listed plugin that raises on
  import is recorded with its error and skipped (the rest still load);
  installed-but-not-allow-listed is skipped silently. *(strategies/plugins.py;
  `docs/plugins/strategy-plugins.md`)*
- **D36 (2026-09-07) — `StrategyConfig.type` widens to `str`; the registry
  boundary owns validation (S3).** The domain Literal becomes `str` (a
  superset — every existing snapshot and YAML stays valid; no migration).
  Typo protection moves to the **create boundary**: API create/update
  validates `strategy.type` against the live registry (422 naming the known
  strategies); `jarvis agent create` does the same with a friendly error.
  The **resolve boundary** stays authoritative for already-pinned versions:
  a snapshot whose strategy is gone fails as a persisted terminal
  `run.failed` with `error_kind="strategy"` (a new value on the frozen
  envelope's existing field, ADR 0003 precedent) — never an exception past
  the runtime, never a worker retry loop. A plugin whose `step` raises is
  caught at the step call site and maps to the same kind.

- **D37 (2026-09-08) — MCP servers are tenant-scoped rows; agents bind
  tool names (S4, ADR 0012).** An MCP server is a `mcp_servers` row —
  tenant-scoped like agents (NULL tenant = shared), slug name unique per
  tenant and **immutable** (thousands of version snapshots join on it),
  typed JSONB config carrying `EnvCredentialRef` values (secrets stay
  references, ADR 0005). `ToolBinding` is unchanged: an agent binds the
  discovered tool name `mcp__<server>__<tool>` exactly like `calculator`;
  the snapshot pins the *name*, the platform resolves connectivity at run
  time — the `credential_ref` precedent. The roadmap sketch's
  binding-config-carries-transport is rejected: the "Tools → MCP server
  management" UI item demands a registry, and one server change shouldn't
  require touching every agent. Server management is admin/owner-only
  (members-route pattern); members list/probe; foreign tenants 404 (D29).
  Discovery is not exposure: binding selection is the allow-list (D35
  philosophy), and unknown/drifted names degrade exactly as builtins
  already do — skipped at prompt build, "unknown tool" error result if
  called.
- **D38 (2026-09-08) — MCP toolsets resolve eagerly per segment, inside
  the runtime's try (S4, ADR 0012).** The D28 pattern's third application
  (model → strategy → tools): each run segment groups its enabled
  `mcp__*` bindings by server, connects, lists tools, and builds a
  per-run registry view (builtins + MCP wrappers) before prompt build —
  inside `run()`/`resume()`'s try, after model resolve. Resolution failure
  (missing, disabled, unreachable, absent env var, protocol error) is a
  persisted terminal `run.failed` with the existing `error_kind="tool"`
  naming the server — never an exception past the runtime, never a worker
  retry loop. Per-call failures after resolve stay recoverable error
  `ToolResult`s through the unchanged `ToolRuntime` envelope (validation,
  timeout, cancellation, truncation apply to MCP for free). Connections
  close with the segment — complete, fail, cancel, or pause; a resumed
  segment re-resolves. MCP descriptors default
  `annotations.requires_approval=True` (binding config can ungate):
  an external server is an arbitrary side-effect surface, and ADR 0011
  already gives the human per-call verdicts. Agents with no `mcp__*`
  bindings resolve nothing — behavior byte-identical to today.

- **D39 (2026-09-09) — MCP headers/env widen to the full CredentialRef
  union; stored secrets resolve at connect time via the shared resolver
  (ADR 0013).** `McpHttpConfig.headers` and `McpStdioConfig.env` go from
  `dict[str, EnvCredentialRef]` to `dict[str, CredentialRef]` — the same
  `env | stored` union the model `credential_ref` uses. No migration (the
  JSONB discriminator gains a variant). The stored secret materializes only
  inside `McpServerConnection._build_client`, per segment, through the
  `CredentialResolver` `AppContainer` already builds for model credentials,
  with the run's `principal` threaded per call (`resolve`/`probe`/
  `_factory_for`). Every failure stays `McpResolutionError`, so the
  existing honest paths fire unchanged (probe → 502 `mcp_unreachable`;
  run → terminal `run.failed` `error_kind="tool"`); a provider built
  without a resolver short-circuits with a clear message on stored refs
  instead of AttributeError. **Rejected**: the provider pre-materializing
  a plaintext `dict[str,str]` into the connection ctor — breaks every
  existing `connection_factory` fake, keeps plaintext alive for the whole
  segment, and needs a second CredentialError→502 mapping. Storage stays
  **signed-in-only** (credentials create 403s anonymous, `created_by` NOT
  NULL); the UI hides the stored option in anonymous mode (never
  show-disabled) with an env-vars hint. UI scope: add + edit refs + rotate
  from the Tools page — edits PATCH the **full config** (wholesale
  replace), rotation is the write-only `{secret}` PATCH with a
  consequence-stating confirm.

- **D40 (2026-09-12) — LLM trace gains a web-readable in-memory buffer
  (ADR 0014).** `JARVIS_LLM_TRACE` keeps logging, and the same
  request/response payloads land in `LlmTraceBuffer` — a bounded
  OrderedDict keyed by run_id (most recent ~20 runs), read by
  `GET /v1/executions/{run_id}/llm-trace` behind the standard scoped
  executions guard (foreign run → 404). Disclosure is
  `sections.executions.detail.llm_trace` (the `human_in_the_loop`
  pattern), not a new capabilities section — `observability` stays
  reserved for S7. Nothing persisted: no table, no event type, restart
  loses it. Distributed mode degrades to log-only (worker processes hold
  the buffer); documented, not engineered around — a DB-backed trace was
  explicitly rejected as reversing the debug-only spirit.

- **D41 (2026-09-14, S6 planning) — Workflow runs ARE
  `agent_executions` rows; the queue gains a `kind` discriminator.** No
  parallel run-shaped table: a workflow run puts the workflow id in the
  row's `agent_id` column, the workflow version id in
  `agent_version_id`, and `metadata.kind = "workflow"`. The entire
  S1/S10 machinery (queue, leases, sweeper rules, pause-reaper,
  cross-process cancel, `PgEventStream`, SSE resume) applies to
  workflow runs unmodified — a separate `workflow_executions` table
  was rejected as duplicating exactly what the roadmap said to reuse.
  The one ports/ change (pre-declared, ADR 0015 §4):
  `RunQueueMessage.kind: Literal["agent", "workflow"] = "agent"` —
  default keeps every existing message byte-identical; the worker
  branches only at its version-resolution site.

- **D42 (2026-09-14, S6 planning) — Agent nodes pin agent versions at
  PUBLISH time.** `update_and_publish` resolves the agent's latest
  version and freezes `agent_version_id` into the workflow snapshot —
  replay fidelity is the D1 argument one level up; a workflow
  version's meaning never changes under it. Rejected: "latest at run
  time" (a republished agent silently changes what a pinned workflow
  version means). The D37 name-join pattern does not apply: MCP names
  are registry join keys, agent versions are content. Stale pins are
  publish-time *lints* (warnings), never failures.

- **D43 (2026-09-14, S6 planning) — `node_id` joins the envelope;
  nodes never emit terminals.** The frozen ADR 0003 envelope gains one
  optional field, `node_id: str | None = None` (backward compatible
  both directions; the amendment is pre-declared in ADR 0015 §3), plus
  two new event types: `node.started` / `node.completed`. Every event
  an executing node emits carries the node's id, stamped by a `NodeSink`
  wrapper that also REFUSES terminal appends — the workflow runtime
  alone finalizes, and the run's event log stays one gapless sequence.
  There is no `node.failed` event type: a node's inner failure becomes
  the run's terminal `run.failed`, error prefixed `node '<id>': ...`,
  inner error kind preserved (one failure, one terminal).

- **D44 (2026-09-14, S6 planning) — Sequential walk over acyclic
  graphs; the executor owns the node cap.** Graphs validate acyclic at
  create/update (422); execution walks from the start node one node at
  a time — deterministic event order, trivially true replay. The
  ADR 0004 rule transposed one level up: `WorkflowRuntime` owns
  `max_node_executions` (default 24), a node owns one step. Parallel
  fan-out, backward edges/loops, and join nodes are deferred WITH the
  design note recorded (ADR 0015 §7) — per-branch sub-contexts with
  merged usage, anticipated by the `node_id`/NodeSink design.

## 4. Explicit deferrals (decided *not* to build in Phase 1)

Redis/queues · plugins & marketplace · multi-tenancy/auth · RAG · workflow
engine · multi-agent · human-in-the-loop · richer memory · evaluation ·
triggers · MCP · parallel tool calls · jinja2 sandboxing · OTel exporters.
Each deferral names its seam in `docs/roadmap.md` (stages S1–S14) — the
deferral is a sequencing decision, not an architectural rejection.
(*Frontend* was on this list until 2026-09-04 — decision 1.6 moved it to a
parallel track: the shell ships now against Phase 1, and each backend stage
enables its UI section as it lands. Since then, multi-tenancy/auth (S2),
human-in-the-loop (S10), and plugin strategies (S3) have landed; MCP (S4)
is planned (ADR 0012); the rest remain staged.)