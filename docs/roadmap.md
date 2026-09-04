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
S1 Distributed runs / queue-backed events ──┬─> S13 Triggers (needs workers)
S2 Auth & multi-tenancy ────────────────────┼─> S5 Frontend builder
S7 OTel tracing (independent)               ├─> S14 Multi-agent (needs S1
S3 Plugin strategies (independent)          │    durability + tool family)
S4 MCP tools (independent) ─────────────────┤
S6 Workflow engine (needs S1) ──────────────┘
S10 Human-in-the-loop (in-process first; durable pause wants S1)
S11 Evaluation framework (independent; richer with S12)
S12 Richer memory (independent)
S8 RAG / knowledge (needs S4 tool family)
S9 Small hardening items (any time)
```

S1–S4, S7, S10–S12 are independent of each other; S5/S6/S8/S13/S14 consume
several. The recommended sequence is S1 → S2 → S3/S4 (either) → S6 → S5 →
S8, with S7 and S9 slotted anywhere; S10–S12 slot in as product needs
demand (S10 early if interactive agents are the priority — it works fully
in-process before S1 lands).

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

## S10 — Human-in-the-loop

**Goal:** runs can pause for human input or approval and resume later —
interactive agents instead of fire-and-forget.

**Rides on:** the event envelope (ADR 0003) and the run-never-raises
contract. Pausing is *not* cancellation: it needs a new terminal-adjacent
state and one new event type — the envelope shape itself stays frozen, so
this stage takes a small ADR.

**Design sketch:**

- New event type `run.awaiting_input` with the pause reason and (for
  tool-approval pauses) the pending call; new `ExecutionStatus`
  `awaiting_input`. Terminal semantics extend: `awaiting_input` is a
  resumable pause — `finalize()` is *not* called; the run's loop returns
  instead, exactly as cancellation does today.
- Two trigger classes:
  1. **Tool approval** — `ToolDescriptor.annotations` gains
     `requires_approval: bool` (annotations are already the sanctioned
     extension point); the tool runtime emits `approval.requested` and
     pauses before executing.
  2. **Strategy-requested input** — a `StepOutcome` variant
     (`AskHumanStep`) lets a strategy stop and ask (clarifying questions,
     missing parameters).
- API: `POST /executions/{id}/resume` accepts `{content | tool_approval:
  true}` → re-enters the same run: history is already persisted, so the
  runtime re-enters the loop with a user/developer message appended and the
  same version pinned. Multiple pauses are fine (a run is a chain of
  pause/resume segments).
- Timeout policy: a paused run has a deadline; expiry → `run.cancelled`
  (reason: `awaiting_input timeout`) so no run is ever stuck. Roadmap S1
  makes the pause durable across processes (in-process Phase-2a: pause
  works, resume must hit the same process).

**Acceptance:** an agent that pauses on an approval-gated tool, resumes
through the API, and completes; `Last-Event-ID` resume works across the
pause; a run paused forever is reaped by the deadline; unit tests cover the
new event type's terminal-adjacency (no non-terminal events after
`awaiting_input` until resume).

---

## S11 — Evaluation framework

**Goal:** score agent versions against test sets, using the data Phase 1
already persists — no new instrumentation needed.

**Rides on:** runs are fully reconstructible (events, messages, tool
executions, usage); agent versions are immutable snapshots, so an eval
always runs against a pinned version.

**Design sketch:**

- `eval_datasets` / `eval_cases` (input, expected, variables) and
  `eval_runs` (dataset × version → many executions, reusing the ordinary run
  path with a batched runner in the worker pool from S1).
- Scorers behind a `Scorer` port: deterministic (regex/json-schema/exact),
  programmatic (tool-sequence assertions), and LLM-as-judge (a scorer is a
  small prompt-engine template + model call — no runtime loop involved).
- Version comparison is a query, not a feature: score distributions across
  `agent_versions` of the same agent.
- API: `/v1/agents/{id}/evals` (create run, list, show scores).

**Acceptance:** an eval run of a mock agent produces scores persisted and
comparable across versions; an eval run's executions are indistinguishable
from manual runs in `/executions` (same event model).

---

## S12 — Richer memory

**Goal:** beyond the flat last-N window Phase 1 ships.

**Rides on:** `MemoryConfig` + `ConversationRepo` already separate
*conversation history* (durable, per session) from the prompt build; memory
is a strategy-adjacent concern the orchestrator already delegates.

**Design sketch (incremental, each independently shippable):**

1. **Summarization/compaction** — when history exceeds
   `max_messages`, a summarizer model call condenses the evicted prefix
   into a rolling `summary` message (a new message role or a
   `MemoryState` row). Deterministic, testable with the mock provider.
2. **Vector memory** — pgvector store behind a `MemoryStore` port;
   semantic recall of past sessions/messages injected into
   `PromptContext`; reuses S8's pgvector infra.
3. **Working memory / scratchpad** — a per-session key-value store
   agents read/write through a builtin tool (`memory_get`/`memory_put`),
   persisted like tool executions.

`MemoryConfig` gains fields (`strategy: window | summarize | vector`,
`store: pgvector | none`) with the current behavior as the default — no
migration for existing agents.

**Acceptance:** a long conversation with summarization keeps the run within
budget while answering a question that requires the *first* turn's content
(the window strategy provably fails this test, the summarizer passes).

---

## S13 — Triggers

**Goal:** runs start without a human calling `/run` — schedules, webhooks,
event watchers.

**Rides on:** S1 (workers + queue) — a trigger is just a producer that
enqueues run requests; the API's run path is already a thin function of
(definition, version, input).

**Design sketch:**

- `triggers` table: `{kind: cron | webhook | event, agent_id, version:
  pinned|latest, input_template, enabled, config}`; a scheduler worker
  (APScheduler or a Postgres-cron loop) enqueues due cron triggers.
- Webhook triggers get an unauthenticated-by-auth endpoints with signed
  payloads (HMAC) — this stage depends on S2's key model for the secret
  handling pattern.
- Event triggers: a small pluggable condition engine ("when a run
  completes with status X", "when a tool emits Y") evaluated by the worker
  that finalized the run — no polling.
- Every trigger-fired run carries `metadata.trigger_id` (the metadata
  field is already persisted on executions), so provenance is queryable.

**Acceptance:** a cron trigger fires a mock agent on schedule through a
worker; a webhook POST enqueues and streams like any run; disabling a
trigger stops runs without deleting history.

---

## S14 — Multi-agent

**Goal:** agents that delegate to agents — supervisor/handoff patterns.

**Rides on:** the Tool family (an agent is "just another tool" — same
pattern as MCP in S4) and `ExecutionContext` (caller identity, shared
`trace_id`).

**Design sketch:**

- **Sub-agent tool**: `sub_agent` tool family — `_execute` invokes
  `AgentRuntime.run` on a referenced agent with a child `ExecutionContext`
  (new `run_id`, shared `trace_id`, caller fields set to the parent run).
  The tool runtime's timeout/cancellation wrappers apply for free; a
  sub-agent's events carry `parent_run_id` — an *additive, optional* field
  on the envelope, which takes an ADR (ADR 0006) since the envelope is
  currently frozen.
- **Handoffs** (agent-to-agent transfer with the conversation) build on the
  same mechanism at the strategy level, not the runtime level.
- Budgets compose: a sub-agent's usage rolls up into the parent's token
  budget via `RunLimits` (ADR 0004's "orchestrator owns limits" — the
  platform clamps the whole tree, not each node).
- Termination guarantees carry over unchanged because every sub-agent run
  is an ordinary run: never raises, exactly one terminal event.
- Depends on S1: nested runs must survive process death like any run.

**Acceptance:** a supervisor agent delegates to two workers via the
sub-agent tool; the parent's token budget bounds the whole tree; a
cancelled parent cancels its children at their next checkpoints;
`/executions/{parent}` links child runs; cursor/resume tests pass per run.

---

## Sequenced build order (recommended)

1. **S1** — durability payoff is highest; every later stage leans on runs
   outliving processes.
2. **S2** — auth before anything user-facing (S5), multi-tenant (S6), or
   externally reachable (S13 webhooks).
3. **S10** — human-in-the-loop early if interactive agents are the priority;
   works fully in-process before S1 makes pauses durable.
4. **S3 + S4** — independent; S4 first if tool breadth matters sooner.
5. **S6** — workflow engine, after S1.
6. **S12 + S11** — richer memory then evaluation, once versions and runs
   accumulate real traffic.
7. **S14** — multi-agent, after S1 and S4/S6 (nested durability).
8. **S5** — frontend, after S2 (and benefits from S6/S10).
9. **S13, S7, S8, S9** — as needed (S13 last of the core set: it wants S1 +
   S2 both in place).

Each stage gets its own implementation-plan-style doc (commit sequence,
gates, tests) at build time, the way Phase 1 did.