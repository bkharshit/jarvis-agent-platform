# JARVIS

An open-source, production-grade AI agent platform. Agent runtime first —
tools, MCP, workflows, RAG, and frontend ride on the interfaces the runtime
establishes.

> Status: Phase 1 (agent runtime) complete; roadmap stages F1 (product
> shell), S1 (distributed runs, ADR 0008), S2 (auth, multi-tenancy,
> BYOK credentials, ADR 0009/0006), S10 (human-in-the-loop,
> ADR 0010), S6 (workflow engine, ADR 0015), S12 (richer memory,
> ADR 0016) and S11 (evaluation framework, ADR 0017) shipped. See
> `docs/roadmap.md` for the stage list and `docs/adr/` for the design
> decisions.
> The frontend ships alongside the backend: the full product shell
> landed first (roadmap F1) and each section enables as its backend
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
| GET | `/executions` | List runs (`agent_id`, `status`, `session_id`, `parent_run_id`, `trigger_id`, `limit`, `offset`) |
| GET | `/executions/{run_id}` | Run + transcript + tool executions |
| POST | `/executions/{run_id}/cancel` | Idempotent cancel — live token in-process, cross-process request row otherwise |
| POST | `/executions/{run_id}/resume` | Answer a paused run (`{content}`, `{tool_approval: bool}`, or per-call `{decisions}`; blocking, mirrors `/run`) — 409 when not awaiting |
| GET | `/executions/{run_id}/events` | Event replay: JSON, or SSE with `Accept: text/event-stream` |
| GET | `/conversations/{agent_id}/{session_id}/messages` | Conversation history |
| GET | `/capabilities` | Section flags + registry-derived detail the UI renders from |
| GET | `/models` | Live model catalog from a provider endpoint (`provider`, optional `base_url`/`api_key_env`/`credential_id`; 502 `model_unreachable`/`model_auth` on failure — ADR 0007) |
| POST/GET | `/auth/login`, `/auth/logout`, `/auth/whoami` | Session auth (S2): opaque httpOnly cookie, server-side session rows |
| GET/POST/PATCH/DELETE | `/members` | Tenant member management — admin/owner only (ADR 0009 §8) |
| GET/POST/DELETE | `/api-keys` | API keys for the acting user — plaintext returned exactly once at create |
| GET/POST/PATCH/DELETE | `/credentials` | BYOK credentials — write-only: the secret never comes back (ADR 0006) |
| GET/POST/PATCH/DELETE | `/workflows` | Workflow definitions (DAGs of agent/tool/condition nodes) — versioned like agents, 409 on duplicate name |
| POST | `/workflows/{id}/publish` | Pin a new version snapshot |
| POST | `/workflows/{id}/run` / `/stream` | Blocking run / SSE stream — same envelope as agents plus `node.started`/`node.completed` with a `node_id` (D43) |
| GET/POST/PATCH/DELETE | `/evaluations/datasets` | Eval datasets — JSONB snapshots of test cases + scorers (+ judge model for `llm_judge`); PATCH is wholesale, `llm_judge` without a judge model is a 422 (D48/D50) |
| POST | `/evaluations/datasets/{id}/runs` | Run a dataset against an agent's latest published version — 202; one ORDINARY queue run per case |
| GET | `/evaluations/runs/{id}` | Eval-run detail: derived status (no column), lazily-scored results persisted once (D49) |
| GET | `/evaluations/compare?agent_id=` | Score aggregation across the agent's pinned versions |
| GET/POST/PATCH/DELETE | `/triggers` | Trigger rules — `cron` / `webhook` / `event` (ADR 0019); PATCH is wholesale (full document), `enabled: false` stops firing without deleting history, delete keeps the runs it fired |
| POST | `/triggers/{id}/rotate-secret` | Re-mint a webhook secret — plaintext returned exactly once, never in a GET |
| POST | `/triggers/{id}/fire` | Fire now (202, run queued; 409 when disabled) — admin/owner |
| POST | `/triggers/webhook/{id}` | UNAUTHENTICATED signed ingest: HMAC-SHA256 `X-Jarvis-Signature` over the raw body — 202 queued, 401 bad/missing signature, 404 absent-or-disabled, 413 oversized |
| GET/POST/PATCH/DELETE | `/knowledge/datasets` | Knowledge datasets (S8/ADR 0020) — the embedding config is IMMUTABLE and dimension-pinned at create (any config key on PATCH is a 422); DELETE is 409 while agent version snapshots reference the dataset |
| GET/POST | `/knowledge/datasets/{id}/documents` | List / synchronously ingest text documents — the POST blocks on chunk → batched embed → one-tx persist → 201 `ready`; embedder failure persists an `error` row and answers 502 `embedding_unavailable` |
| GET/DELETE | `/knowledge/datasets/{id}/documents/{doc_id}` | Read / delete one ingested document |

Every error has one envelope shape: `{"error": {"kind", "message", "details"}}`.

### Auth and multi-tenancy (S2, ADR 0009)

Auth is a config choice. `JARVIS_AUTH_MODE=anonymous` (the default) keeps
local dev friction-free: every request acts on a fixed `default` tenant.
`JARVIS_AUTH_MODE=required` 401s unauthenticated requests — sign in with a
session (browser) or present an API key (`Authorization: Bearer
jarvis_sk_…`). Callers resolve to a tenant-scoped Principal: agents,
executions, conversations, members, keys, and credentials are all isolated
per tenant, and a foreign id reads as 404, never 403 (no existence leak).

The first principal is provisioned by the CLI (no authenticated route can
provision the principal that would authenticate it):

```bash
uv run jarvis tenant create acme "Acme Corp"
uv run jarvis user create acme owner@acme.test --role owner --password s3cret
uv run jarvis api-key create owner@acme.test --name cli   # plaintext printed once
```

### BYOK credentials (S2, ADR 0006)

Model credentials are a `credential_ref` union on the agent definition: an
`env` ref names an environment variable (self-hosted default, ADR 0005), a
`stored` ref points at an encrypted, tenant-owned credential:

```yaml
model:
  provider: openai_compatible
  model: gpt-4o-mini
  credential_ref: {type: stored, credential_id: <id from POST /v1/credentials>}
  # or: credential_ref: {type: env, env_var: OPENAI_API_KEY}
```

Stored secrets are AES-GCM-encrypted at rest (master key from the
environment; config stores only the env var *name*) and are write-only
through the API: the secret enters on create/update and is never returned
by GET, never logged, never snapshotted. A credential failure ends the run
as a persisted terminal `model` failure — a cross-tenant id resolves to
"not found", exactly like any other foreign resource.

The same union backs MCP server auth headers (ADR 0013): an http server's
`config.headers` maps header names to refs — `{"type": "env",
"env_var": "WEBZ_MCP_TOKEN"}` or `{"type": "stored", "credential_id":
<id>}` — resolved at connect time, secrets never stored in the config.
Header-auth MCP servers are configurable end to end from the Tools page
(add, edit refs, rotate the stored secret).

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

### Human-in-the-loop (S10, ADR 0010)

Runs can pause for a human decision instead of failing or guessing. Two
trigger classes:

- **Tool approval** — a tool binding carries
  `config: {requires_approval: true}`; the model's gated calls emit
  `tool.call.requested` but never start: the run pauses with
  `status: "awaiting_input"` and the pending batch on the durable
  `run.awaiting_input` event.
- **Strategy-asked input** — a strategy step returns `AskHumanStep`
  (clarifying questions, missing parameters); the run pauses with a
  question.

A pause is a *segment*, not a terminal: HTTP callers see a 200 with
`status: "awaiting_input"` (`finished_at: null`), the SSE stream ends at
the pause frame exactly like a terminal, and the gapless event sequence
continues when the run resumes. Answer through the resume route —
validated to exactly one of:

```bash
curl -s -X POST localhost:8000/v1/executions/<run-id>/resume \
  -H 'Content-Type: application/json' -d '{"tool_approval": true}'
# or: -d '{"content": "Harshit"}'   (a strategy-asked question)
# or per-call (ADR 0011): approve c1, decline c2 — a call absent from
#     the map is declined, never run
curl -s -X POST localhost:8000/v1/executions/<run-id>/resume \
  -H 'Content-Type: application/json' \
  -d '{"decisions": {"c1": true, "c2": false}}'
```

Approve executes the gated batch; reject closes the declined calls with a
refusal tool message (they never ran — the message tells the model not to
call the tool again and to answer from what it knows) and lets the model
continue. With `decisions` (ADR 0011) the gated batch is split per call:
approved calls execute, declined ones get the refusal message, and calls
absent from the map are declined — silence is never approval. The resume
is blocking — it returns the row for the resumed segment's end, which may
pause again. Limits span the chain: `max_iterations` and the token budget
bound the whole run, not one segment.

A paused run carries a deadline (`awaiting_until`, default 24h,
`JARVIS_AWAITING_INPUT_TIMEOUT_SECONDS`); the worker's sweeper reaps
expired pauses with the one terminal `run.cancelled` (reason
`awaiting_input timeout`) so no run is ever stuck. Cancelling a paused
run is immediate — nothing holds it. On the web, the run console renders
the pause card (per-call approve/reject selection with an allow-all
shortcut / answer form), the Executions section gains an awaiting-input
inbox, and an `awaiting_input` run's detail page offers the same pause
card plus Cancel run — answer a durable pause from wherever you find it.
All of it is gated on `executions.detail.human_in_the_loop` from
`/v1/capabilities`.

### Plugin strategies (S3, D35/D36)

The loop recipe — how an agent thinks, turn by turn — is pluggable. Ship a
new strategy (plan-and-execute, tree-of-thoughts, reflection loops, …) as
an ordinary pip package that declares an entry point:

```toml
[project.entry-points."jarvis.strategies"]
plan_execute = "my_pkg.strategies:PlanExecute"
```

Nothing loads by default: a strategy runs only if its name is on
`JARVIS_STRATEGY_PLUGIN_ALLOWLIST` (comma-separated; install the package,
set the list, restart — there is no hot load). The CLI wraps the flow:

```bash
jarvis plugin new my-strategy          # scaffold a ready-to-edit package
jarvis plugin install ./my-strategy    # install + allow-list + restart hint
```

At boot the loader records
import failures and absent names instead of crashing, and `/v1/capabilities`
reports every strategy with its origin (`builtin`/`plugin`), distribution,
and version — the **Plugins** page in the web UI renders exactly that, and
the agent editor's strategy dropdown offers whatever is loaded.

The boundaries refuse, honestly: creating an agent with an unknown strategy
type is a 422 (API) / exit 1 (CLI) naming the known set; a run whose pinned
version references a plugin that is no longer loaded ends as a persisted
`run.failed` with `error_kind: "strategy"`; a plugin whose `step()` raises
gets the same treatment — a run never escapes the runtime. The full
third-party contract (one model invocation per step, never loop, never emit
terminal events, forward `text.delta` for a live transcript) is pinned in
`docs/plugins/strategy-plugins.md`, with working samples in
`tests/fixtures/strategies/jarvis-strategy-fixtures` (`plan_execute`,
`tree_of_thoughts`) and a live walkthrough in `docs/walkthrough-s3.md`.

### MCP tools (S4, ADR 0012)

Any MCP server — stdio subprocess or streamable-http — is a tool
provider. Servers are tenant-scoped registry rows managed over the API;
nothing about connectivity ever lives in the agent definition. An agent
binds a tool by NAME, `mcp__<server>__<tool>`, exactly like a builtin,
and the platform resolves the server at run time:

```bash
# register (config carries NAMES only — secrets are env refs or stored
# credential ids, see BYOK above; ADR 0013)
curl -X POST localhost:8000/v1/mcp/servers -H 'content-type: application/json' \
  -d '{"name":"fixtures",
       "config":{"type":"stdio","command":"python","args":["server.py"]}}'

curl -X POST localhost:8000/v1/mcp/servers/<id>/probe   # connect fresh, list tools
```

The semantics that matter:

- **Discovery is not exposure.** The probe lists what a server offers;
  only tools an agent actually binds ever register at run time — the
  binding selection IS the allow-list.
- **Approval is the default.** Every discovered descriptor carries
  `requires_approval: true`; un-gating a tool is an explicit per-binding
  choice (`config.requires_approval: false`, the S10 binding-wins rule).
- **Resolution is eager, per segment, inside the runtime** (D38, the D28
  pattern): a missing, disabled, or unreachable server ends the run as a
  persisted terminal `run.failed` with `error_kind: "tool"` naming the
  server — before a single token is spent, never a 500. Per-call
  failures stay recoverable error `ToolResult`s.
- **Connections close with the segment; a resume re-resolves.** Deleting
  a server row leaves version snapshots intact (D1) — the next run of a
  bound agent just fails resolution, honestly.

In the web UI the **Tools** page manages servers (add with credential
picker, probe, enable/disable, remove), and the agent editor's "Add MCP
tool" picker lists a server's tools with a Requires-approval toggle. A
live walkthrough (real stdio fixture server, pause→approve→resume, delete
semantics, stored-header §7) is in `docs/walkthrough-s4.md`.

### Workflows (S6, ADR 0015)

A workflow is a DAG of **agent**, **tool**, and **condition** nodes that
runs through the exact machinery an agent run uses — same queue, same
event envelope, same persistence. A workflow run IS an execution row
(`metadata.kind: "workflow"`); the only envelope addition is an optional
`node_id` on `node.started`/`node.completed` events (D43). Versioning
mirrors agents: snapshots are append-only, and an agent node's
`agent_version_id` is **pinned at publish** — a stale pin is a lint
warning in the detail's `lints`, never a failure (D42; no pin target at
all is a 422 at save).

```bash
curl -X POST localhost:8000/v1/workflows -H 'content-type: application/json' \
  -d '{"name":"chain",
       "nodes":[{"id":"a","type":"agent","config":{"agent_id":"<agent-id>","input_template":"{{input}}"}},
                {"id":"b","type":"agent","config":{"agent_id":"<agent-id>","input_template":"{{node.a}}"}}],
       "edges":[{"from_node":"a","to_node":"b"}],
       "start_node_id":"a"}'

curl -X POST localhost:8000/v1/workflows/<id>/run \
  -H 'content-type: application/json' -d '{"input": "go"}'
```

The semantics that matter:

- **Templates are plain `{{var}}` substitution** — `{{input}}` is the run
  input, `{{node.<id>}}` is an upstream node's output (the output IS the
  value). No Jinja; the graph is validated (acyclic, resolvable node ids)
  at create/update — 422, never a runtime surprise.
- **Sequential walk with a cap** (D44): `max_node_executions` (default
  24, 1–128) stops runaway walks as a persisted `run.failed`, not an
  exception. Parallel fan-out and loops are deferred with a design note
  in ADR 0015 §7.
- **No `node.failed`** (D43): an inner failure surfaces as the run's
  terminal `run.failed` prefixed `node '<id>':` — terminals are
  run-level, never node-scoped.
- **HITL composes**: an approval-gated tool inside a node pauses the run
  mid-node (`run.awaiting_input` while the node is open) and the resume
  continues the same walk — S10 unchanged.

In the web UI the **Workflows** section is the React Flow canvas: node
palette, a config panel per node (agent picker, tool bindings, condition
routes), editable node ids (renames rewrite every `{{node.<id>}}`
reference, edge, and condition target), inline agent creation, a
server-hash concurrent-edit guard, and the run console rendering node
groups. A live walkthrough is in `docs/walkthrough-s6.md`.

### Richer memory (S12, ADR 0016)

Conversation memory gains a strategy. `window` (the default, and what
every pre-S12 snapshot drafts as) is the flat last-N slice; `summarize`
compacts the evicted prefix into a rolling per-session summary through
the agent's own model client — compaction consumes a turn, the summary
rides the prompt as a system message, and a summarizer failure degrades
to the plain window (memory can never fail a run). Alongside it, a
per-session **scratchpad** (`memory_get` / `memory_put` / `memory_delete`
builtins, bindable by any agent) gives the model working memory — keys
are the caller's contract, a run without a session fails the tool call
honestly rather than the run.

```bash
curl -X POST localhost:8000/v1/agents -H 'content-type: application/json' -d '{
  "name": "summarizer", "strategy": {"type": "function_calling"},
  "memory": {"enabled": true, "max_messages": 2, "strategy": "summarize"},
  "tools": [{"name": "memory_get"}, {"name": "memory_put"}, {"name": "memory_delete"}]
}'
```

### Evaluations (S11, ADR 0017)

Evaluations are test-case datasets scored over ordinary runs. A dataset
is a JSONB snapshot of cases (`input` / `expected`) plus scorers; an
eval run fans out one ORDINARY queue run per case, pinned to the
agent's latest published version — the children are indistinguishable
from manual runs in `/executions`. Status is derived (no column) and
scoring is lazy: the first completed detail read scores and persists
each result exactly once. Scorers: `exact`, `contains`, `regex`,
`json_schema`, `tool_sequence` (deterministic, against the final
message and tool order) and `llm_judge`, which requires a dataset-level
`judge_model` (a 422 at the boundary) and persists `passed=null` with
the judge's failure detail when it cannot reach a verdict.

```bash
curl -X POST localhost:8000/v1/evaluations/datasets -H 'content-type: application/json' -d '{
  "name": "smoke",
  "cases": [{"id": "c-1", "input": "say hi", "expected": "Hello!"}],
  "scorers": [{"name": "exact"}],
  "judge_model": {"provider": "openai_compatible", "model": "llama3.1",
                  "base_url": "https://ollama.com/v1",
                  "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}}
}'
curl -X POST localhost:8000/v1/evaluations/datasets/<id>/runs \
  -H 'content-type: application/json' -d '{"agent_id": "<id>"}'
curl localhost:8000/v1/evaluations/runs/<eval-run-id>   # first read scores
```

The web **Evaluations** section covers dataset CRUD (wholesale-PATCH
editor), eval runs with per-case score chips linking each child run,
and a per-version Compare view. A live walkthrough is in
`docs/walkthrough-s11.md`.

### Sub-agents and handoffs (S14, ADR 0018)

Agents delegate to agents. A tool binding named `sub__<target>` spawns a
child run the parent's tool call blocks on; `handoff__<target>` transfers
the conversation to a successor run and ends the predecessor. Bindings
carry only `{agent_id}` in config — the server stamps the pinned
`agent_version_id` at save/publish (the client never supplies it), detects
cycles at publish (A→B→A is a 422), and lints stale pins instead of
failing. Children are ordinary durable runs: they appear in
`/executions` (filter by `parent_run_id`), share the parent's `trace_id`,
have no session, inherit the parent's deadline, and never pause — an
approval a child needs is auto-declined honestly and the run completes.
The child's usage folds into the parent's budget (ADR 0004 clamps the
whole tree); depth is capped (`JARVIS_SUB_AGENT_MAX_DEPTH`, default 2)
with an honest refusal past it.

```bash
curl -X POST localhost:8000/v1/agents -H 'content-type: application/json' -d '{
  "name": "supervisor",
  "model": {"provider": "openai_compatible", "model": "gemma4:31b",
            "base_url": "https://ollama.com/v1",
            "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}},
  "strategy": {"type": "function_calling"},
  "tools": [{"name": "sub__researcher", "config": {"agent_id": "<researcher-id>"}}]
}'
# run it, then see the children:
curl "localhost:8000/v1/executions?parent_run_id=<run-id>"
```

The web **Agents** editor gets *Delegate to sub-agent* / *Hand off to
agent* pickers (self excluded), and **Executions** detail pages link
children ("Sub-agent runs") and the parent run both ways. A live
walkthrough is in `docs/walkthrough-s14.md`.

### Triggers (S13, ADR 0019)

Runs start without a human calling `/run`. Three trigger kinds in one
tenant-owned table: `cron` (five-field croniter schedule, UTC, fired
by an embedded Postgres-truth scheduler loop), `webhook` (the ONE
unauthenticated ingest — HMAC-SHA256-verified, the trigger row is the
trust boundary), and `event` (watchers on run outcomes, evaluated once
at every terminal path's finalize). A fired run is an ordinary durable
run — same worker, same envelope — plus an indexed `trigger_id` column
for provenance, so a deleted trigger's history stays filterable. Cron
firing is claim-first at-most-once (`FOR UPDATE SKIP LOCKED`):
downtime catches up as ONE fire, and a miss (target agent gone, no
published version) is an honest `last_error` on the row, never a retry
storm. Event chains are impossible by construction — fired runs carry
`metadata.trigger.depth` and the finalize observer refuses to re-fire
past `JARVIS_TRIGGER_MAX_DEPTH` (default 1), reading the persisted row.
Webhook secrets are AES-GCM-stored (D30) and returned as plaintext
EXACTLY ONCE (create/rotate), never in a GET.

```bash
# a cron trigger
curl -X POST localhost:8000/v1/triggers -H 'content-type: application/json' -d '{
  "name": "hourly-tick", "kind": "cron", "agent_id": "<agent-id>",
  "version_policy": "latest", "input_template": "Hourly tick at {{now}}.",
  "config": {"type": "cron", "schedule": "0 * * * *"}}'

# a signed webhook ingest — no auth header; the signature IS the auth
BODY='{"alert":"disk 91% on db-1"}'
SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$(cat /tmp/whsec)" | awk '{print $NF}')"
curl -X POST localhost:8000/v1/triggers/webhook/<trigger-id> \
  -H "X-Jarvis-Signature: $SIG" --data-binary "$BODY"   # → 202 {"run_id", "status": "queued"}
```

The web **Triggers** section manages all three kinds — create/edit/
enable-toggle/fire-now/delete, the webhook ingest URL derived
client-side, the secret shown once behind a dismiss banner, rotate
re-mints it — and trigger-fired runs show a **Fired by** chip linking
to the filtered executions list. A live walkthrough is in
`docs/walkthrough-s13.md`.

### Knowledge and RAG (S8, ADR 0020)

Agents can know things. A **knowledge dataset** is a set of embedded
text chunks (paragraph-aware chunking over pasted or client-side-read
text documents — no multipart, no PDFs) stored in pgvector. An agent
binds datasets in its definition and gets retrieval either way:

- **The knowledge binding** (auto-injection): before each turn the
  runtime retrieves top-k chunks for the user's input and injects them
  as a system message (`Relevant knowledge base excerpts: …`) between
  the summary and the history — mirrored in resume rebuilds, so a
  resumed segment retrieves the same way.
- **The `retriever__<dataset>` tool family** (model-directed): the
  model calls retrieval through the ordinary tool loop, the result
  rendered as `[doc] content` blocks.

Both ride ONE retrieval service behind the runtime (`retrieve(config,
query, tenant) → rendered context`), so hybrid retrieval / rerank /
metadata filtering can land later without touching the agent runtime
(D68). The dataset's embedding config (provider, model, base_url,
credential, dimension) is **immutable and dimension-pinned** at create —
"change the embedding model" means a new dataset; all datasets bound to
one agent must share one embedding setup (a query embedding serves them
all; mixed bindings are a save-time 422). Ingestion is synchronous and
honest: the POST blocks on chunk → embed → persist; a dead embedder
persists an `error` document row and answers 502, never a silent 201.
Retrieval failure against a stale snapshot is a terminal `error_kind=
"tool"` run failure (D38), and a dataset DELETE is 409-refused while
agent version snapshots reference it.

```bash
# create a dataset (any OpenAI-compatible /v1/embeddings endpoint —
# local Ollama serves it; ollama.com does NOT serve embeddings)
curl -X POST localhost:8000/v1/knowledge/datasets -H 'content-type: application/json' -d '{
  "name": "jarvis-notes",
  "embedding_config": {"provider": "openai_compatible", "model": "qwen3-embedding:0.6b",
    "base_url": "http://localhost:11434/v1", "dimension": 1024, "distance": "cosine"},
  "chunk_config": {"chunk_chars": 800, "overlap_chars": 100}}'

# ingest a text document — synchronous, 201 when ready
curl -X POST localhost:8000/v1/knowledge/datasets/<id>/documents \
  -H 'content-type: application/json' \
  -d '{"name": "platform.md", "content": "JARVIS is an agent-runtime-first platform."}'
```

The web **Knowledge** section manages datasets and documents (paste or
file-read ingest, status chips, error rows shown honestly); the agent
editor has a knowledge multi-select + top-k picker.

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