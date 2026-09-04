# JARVIS — Phase 2+ Roadmap

> Status: planning doc. Phase 1 (agent runtime, all 21 plan commits) is
> complete — see `docs/implementation-plan.md` for what shipped and
> `docs/adr/` for the decisions it rests on.
>
> Source of truth for this doc: the §10 "risks & deliberate deferrals" list
> of `implementation-plan.md` and the phase summary in
> `docs/architecture/overview.md`, expanded with the concrete designs
> discussed across build sessions.

**Ground rule** (same as Phase 1): every stage here rides on a seam that
already exists as a `ports/` Protocol. Nothing in Phase 1 was built in a way
that requires rewriting for these stages. Changing a Phase 1 contract
(`EventSink`, `StrategyRegistry`, `Tool`, `ExecutionContext`, …) requires a
new ADR first.

## Stage order and dependencies

```
S1 Distributed runs / queue-backed events ──┐
S2 Auth & multi-tenancy ────────────────────┼──> S5 Frontend builder
S7 OTel tracing (independent)               │
S3 Plugin strategies (independent)          │
S4 MCP tools (independent) ─────────────────┤
S6 Workflow engine (needs S1 for durability)┘
S8 RAG / knowledge (needs S4 tool family)
S9 Small hardening items (any time)
```

S1–S4, S7 are independent of each other; S5/S6/S8 consume several. The
recommended sequence is S1 → S2 → S3/S4 (either) → S6 → S5 → S8, with S7 and
S9 slotted anywhere.

---

## S1 — Distributed runs & queue-backed event bus

**Goal:** move runs out of the API process so a run survives a server
restart *in progress*, and the event bus scales beyond one process — without
changing the API surface.

**Rides on:** `ports/events.py` (`EventSink`, `EventStream`) — Phase 1's
in-process bus was a deliberate placeholder (ADR 0003); the global
`execution_events.cursor` BIGSERIAL already assumes a multi-writer future,
and `Last-Event-ID` resume already survives restarts for *finished* runs.

**Design sketch:**

- Introduce a durable *run queue*: on `POST /run|/stream`, the API writes a
  `queued` execution row + enqueue message, a worker pool picks it up.
- Event fan-out: the worker's `EventSink` appends to Postgres (unchanged);
  live subscribers get pushed via Postgres `LISTEN/NOTIFY` first (zero new
  infra), Redis Streams when cross-node pub/sub is needed.
- The SSE route becomes: subscribe to live bus if the run is owned by this
  process; otherwise tail the DB by cursor until a terminal event. Cursor
  semantics do not change — `/stream` resume already only relies on the DB.
- Cancellation becomes cross-process: a `cancel` writes to the queue; the
  owning worker's token triggers at the next checkpoint (cancellation stays
  cooperative as it is today).

**Work items:**

1. `ExecutionStatus` gains `queued` (alembic migration: alter the
   `execution_status` enum — remember 0001's enum gotcha).
2. `QueuePort` (enqueue/claim/ack) + Postgres-backed adapter (`SELECT … FOR
   UPDATE SKIP LOCKED`) — Redis adapter later if needed.
3. Worker entrypoint (`jarvis worker`) sharing `AppContainer` with the API.
4. `LISTEN/NOTIFY` fan-out adapter implementing `EventStream`.
5. Cancel path: `POST /executions/{id}/cancel` publishes instead of poking
   in-process tokens.

**Acceptance:** kill the API server mid-run; a worker finishes the run; an
SSE client reconnecting with `Last-Event-ID` observes no gaps and no
duplicates. Existing integration suite (SSE exactly-once, terminal
semantics) passes against the distributed sink unchanged.

---

## S2 — Auth & multi-tenancy

**Goal:** callers, API keys, and per-tenant isolation.

**Rides on:** `ExecutionContext` already carries `user_id` (and `session_id`)
end-to-end and persists them on runs; the API layer is a thin transport, so
middleware is the only injection point.

**Design sketch:**

- `tenants`, `users`, `api_keys` tables; agents get a `tenant_id` (nullable
  = platform-shared in single-tenant mode).
- FastAPI dependency `AuthContext` replaces anonymous access: `Authorization:
  Bearer <api-key>` → principal; key hashing at rest (same pattern as ADR
  0005's env-var indirection — keys never in config).
- All repos gain tenant scoping at the repository boundary (WHERE clauses,
  not post-filters); `ExecutionContext.caller_*` fields filled by the
  runtime from the authenticated principal.
- Tool-level secrets stay behind `ToolContext` + `ToolDescriptor.annotations`
  (already the sanctioned place per Phase 1).

**Acceptance:** a tenant cannot read or run another tenant's agents or
executions (404, not 403 — no existence leak); anonymous mode remains a
config choice for local dev; all Phase 1 tests pass with a single shared
tenant.

---

## S3 — Plugin strategies

**Goal:** third-party loop strategies (e.g. plan-and-execute, tree-of-thought)
installed without touching the core.

**Rides on:** `ports/strategy.py` (`AgentStrategy`, `StepOutcome`,
`StrategyRegistry`) — the registry already resolves by name from
`StrategyConfig.type`.

**Design sketch:**

- Built-ins registered explicitly; plugins discovered via package
  entry-points (`jarvis.strategies`), opt-in through a Settings allow-list
  (never auto-load from the filesystem).
- A plugin contract doc pinning the rules strategies already must obey:
  one step per call, never raise past the runtime, emit deltas through the
  sink, terminal events belong to the orchestrator.
- `StrategyConfig.params` already flows to the strategy — plugin params
  need no model change.

**Acceptance:** a sample plugin strategy (in a test fixture package) runs an
agent through the API with zero core changes; malformed strategies still
cannot crash a run (the orchestrator's never-raise guarantee holds).

---

## S4 — MCP tools

**Goal:** connect MCP servers as tool providers.

**Rides on:** `ports/tools.py` — Phase 1 explicitly planned MCP as "just
another Tool family behind `Tool`".

**Design sketch:**

- `McpToolProvider` implements `ToolRegistry`: discovers tools from an MCP
  server (stdio or streamable-http transport), wraps each as a `BaseTool`
  whose `_execute` is a JSON-RPC `tools/call`; `ToolDescriptor` is built from
  the server's schema (the tool runtime's validation/timeout/cancellation
  wrapper then applies to MCP tools *for free* — that is the payoff of the
  template-method design).
- Naming: `mcp__<server>__<tool>` to avoid collisions; allow-lists per
  binding as with `http_get`.
- Agent YAML gains a `mcp` bindings section; the `ToolBinding.config` field
  (already free-form) carries server/transport settings.
- Secrets for remote servers via env-var indirection (ADR 0005 pattern).

**Acceptance:** e2e test with a fixture MCP server exposing one tool; an
agent uses it through `/run` like a builtin; tool executions appear in
`tool_executions` rows identically to builtins.

---

## S5 — Frontend builder

**Goal:** the Phase 2 of the overview — visual agent builder.

**Design sketch (unchanged from earlier discussions):** React + Vite +
React Flow; the backend is already sufficient (agents CRUD, versions, SSE).
A thin mirror service exposes the *same* registry data the backend uses
(tool descriptors, strategy names, config schemas) so the canvas validates
against reality. SSE resume (`Last-Event-ID`) gives the run console its
stream for free. Multi-tenancy (S2) should land first or with it.

---

## S6 — Workflow engine

**Goal:** DAG/graph runs beyond the single-agent loop.

**Rides on:** the deliberate Phase 1 decision that workflows are a *sibling
executor* reusing the event model, persistence, and limits — not a new
paradigm.

**Design sketch:**

- `WorkflowDefinition` (nodes: agent-run, tool, condition; edges) versioned
  with the same append-only snapshot scheme as agents.
- `WorkflowRuntime` mirrors `AgentRuntime`'s contract: never raises, exactly
  one terminal event, limits owned by the orchestrator; node events are
  ordinary `ExecutionEvent` subtypes with a `node_id` field added to the
  envelope (envelope shape is frozen — only new event *types* are added).
- Runs persist through the same tables; replay/reconstruction works
  unchanged; SSE resume works unchanged because cursors are global.

**Acceptance:** a two-node workflow runs through the API with SSE; a failed
node yields `run.failed` with the node id; cursor/resume tests pass
verbatim.

---

## S7 — OTel tracing

**Goal:** distributed traces for runs.

**Rides on:** `trace_id` is already generated, plumbed, and persisted on
every execution row — this stage is pure addition.

**Design sketch:** context-propagate `trace_id` → W3C `traceparent`;
instrument the runtime (span per iteration/model call/tool call), sink
span-links to the event cursor. Exporter behind Settings (`JARVIS_OTEL_*`),
off by default.

---

## S8 — RAG / knowledge

**Goal:** retrieval-augmented agents.

**Rides on:** `ContentPart` is already a union (text/image reserved) and
tools are the sanctioned extension point.

**Design sketch:** a `retriever` Tool family (vector store behind a port,
pgvector first since Postgres is already there) plus a `knowledge` binding
section in the agent definition that injects retrieved context into the
prompt through `PromptEngine` — no runtime loop changes.

---

## S9 — Small hardening items (grab-bag, any time)

| Item | Seam | Note |
| --- | --- | --- |
| Parallel tool calls | capability flag exists on the model layer | runtime executes sequentially today; flip per-agent |
| Jinja2 sandboxing | `prompt/` PromptEngine is isolated | required only when user-supplied templates are exposed (S5) |
| Redis event bus | `EventSink` port | only if S1's Postgres fan-out proves insufficient |
| `make test-db` without Docker | conftest already self-bootstraps `jarvis_test` | document/test the path on CI |

---

## Sequenced build order (recommended)

1. **S1** — durability payoff is highest; every later stage leans on runs
   outliving processes.
2. **S2** — auth before anything user-facing (S5) or multi-tenant (S6).
3. **S3 + S4** — independent; S4 first if tool breadth matters sooner.
4. **S6** — workflow engine, after S1.
5. **S5** — frontend, after S2 (and benefits from S6).
6. **S7, S8, S9** — as needed.

Each stage gets its own implementation-plan-style doc (commit sequence,
gates, tests) at build time, the way Phase 1 did.