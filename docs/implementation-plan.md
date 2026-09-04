# JARVIS — Implementation Plan (Phase 0 + Phase 1)

> Status: **complete** (all 21 commits landed on `main`, 2026-09-04). This
> doc is the historical Phase 0/1 plan, preserved as written; the living
> plan is `docs/roadmap.md`. Note: its "frontend is Phase 2 — not started"
> line was superseded on 2026-09-04 by decision 1.6 — the frontend now
> ships alongside the backend (roadmap F1).
> Reference: Dify clone at `./dify-reference` (read-only, HEAD Sept 2026) — used as architectural reference only.

## Context

We are building an open-source, production-grade AI agent platform (**JARVIS**) in this repository (greenfield), inspired by but not copying Dify.

Governing philosophy:
- Build an excellent **agent runtime first**; tools platform, MCP, workflows, RAG, frontend, plugins, multi-agent all ride on interfaces the runtime establishes now.
- Modular monolith; no premature microservices; runtime fully testable from Python with no frontend and no LLM (mock provider/tools).
- Every execution has a run ID, emits events, and is persisted well enough to reconstruct what happened.
- Do NOT over-engineer Phase 1: no plugins/marketplace, multi-tenancy, RAG, workflows, multi-agent.

**Key finding from Dify exploration that motivates this design**: the Dify checkout is mid-migration — its agent runtime was extracted into a separate `dify-agent` service (loop now delegated to pydantic-ai), the workflow engine/model runtime moved into an external `graphon` pip package, and legacy ReAct/FC runners survive in `api/core/agent/`. Dify is paying a large refactoring cost because early interfaces weren't clean. We get the seams right in Phase 1.

## Confirmed decisions

- **Name**: JARVIS — package `jarvis/`, CLI `jarvis`
- **Database**: **PostgreSQL from day one** (docker compose for dev + integration tests; pure-domain unit tests never touch the DB)

## Technology stack

- Python 3.12+, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.x (async, asyncpg) + Alembic, Typer + rich (CLI), httpx + respx (adapter + its tests), pytest + pytest-asyncio, ruff + mypy (`--strict` on domain/ports)
- Model access: one **OpenAI-compatible adapter** via httpx against `{base_url}/chat/completions` — `base_url` injection makes it serve OpenAI, Ollama (`http://localhost:11434/v1`), vLLM, LM Studio; plus a **MockModelProvider** (`provider: "mock"`) for tests/CLI demo
- Redis deferred (in-process event bus in Phase 1; `EventSink` port anticipates queues); frontend (React/Vite/React Flow) is Phase 2 — not started

## Repository layout (end state of Phase 1)

```
my-agent-platform/
├── docs/
│   ├── architecture/{overview,event-model,data-model,model-layer,api}.md
│   ├── reference/{dify-map.md,dify.md}
│   ├── adr/0001..0005
│   └── implementation-plan.md          # this file
├── src/jarvis/
│   ├── domain/            # pure Pydantic, no IO — agent, message, events, execution, tools
│   ├── ports/             # Protocols ONLY (imports pydantic/stdlib only)
│   │   ├── model.py       #   ModelProvider, ModelClient, ModelProviderFactory
│   │   ├── tools.py       #   Tool, ToolRegistry, ToolRuntime
│   │   ├── strategy.py    #   AgentStrategy, StepOutcome, StrategyRegistry
│   │   ├── events.py      #   EventSink, EventStream
│   │   └── repository.py  #   AgentRepo, ExecutionRepo, ConversationRepo
│   ├── models/            # base, request types, openai_compatible, mock, errors, capabilities
│   ├── tools/             # base (template method), registry, runtime, builtin/
│   ├── prompt/engine.py   # PromptEngine + PromptContext
│   ├── strategies/        # function_calling.py, react.py
│   ├── runtime/           # agent_runtime.py (orchestrator), limits.py
│   ├── events/bus.py      # InProcessEventSink (+ replay via repo)
│   ├── persistence/       # models.py, repositories.py, migrations/ (alembic)
│   ├── api/               # app.py, deps.py (AppContainer DI), routes/, sse.py, errors.py
│   ├── cli/               # commands: init, serve, agent, run, executions, doctor
│   └── config.py          # typed Settings
├── tests/{unit,integration}/
├── compose.yaml (postgres), pyproject.toml, Makefile, README.md
```

**Dependency rule (lint-enforced)**: `domain/` and `ports/` import only pydantic/stdlib; everything else depends inward.

## Phase 0 deliverables

- `docs/architecture/overview.md` — layering, module boundaries, stable-interface list, phase roadmap summary
- `docs/architecture/{event-model,data-model,model-layer,api}.md` — specs matching sections below
- `docs/reference/dify-map.md` + `dify.md` — the verified-path map and adopt/simplify/diverge table (content below)
- ADRs: 0001 Postgres day one · 0002 typed Pydantic-over-JSONB persistence (vs Dify's LongText strings) · 0003 event envelope + exactly-one-terminal semantics · 0004 orchestrator-owns-limits / strategy-owns-step · 0005 single OpenAI-compatible adapter via base_url injection
- One-page stubs for future-phase docs (workflow-engine, knowledge, frontend-architecture, observability) stating only the anticipated seam

## Dify reference map (verified paths → `docs/reference/dify-map.md`)

Critical context: this checkout is **mid-migration** — agent runtime extracted to `dify-agent/` (delegates loop to pydantic-ai), workflow engine/model runtime moved to external `graphon==0.7.0` pip package, Go `dify-agent-runtime/` for sandboxed shell; legacy ReAct/FC runners remain in `api/core/agent/`. The repo shows both "before" (in-process loop) and "after" (external service) — and the migration cost itself is the lesson.

| Our subsystem | Dify reference (verified) | Teaches |
|---|---|---|
| Agent loop | `dify-agent/src/dify_agent/runtime/{runner,run_scheduler,agent_factory}.py`; legacy `api/core/agent/{cot,fc}_agent_runner.py` | Process-local scheduler + asyncio supervisor; explicit loop with max-iterations and forced-final-answer on last iteration; terminal events committed atomically (exactly one wins) |
| Agent strategies | `api/core/agent/strategy/base.py`; new `dify-agent/src/agenton/layers/base.py` + `compositor/providers.py` | Strategy must be a genuine extension point; Dify evolved from enum+subclass to compositional registry keyed by `type_id` |
| Model abstraction | graphon model_runtime (external); `api/core/model_manager.py`; `api/core/plugin/impl/model_runtime.py` | Separate generate/stream (avoid Dify's triple-overloaded `invoke_llm(stream=...)` union); capability flags; typed error mapping |
| Tool system | `api/core/tools/__base/{tool,tool_provider,tool_runtime}.py`; five families in `api/core/tools/` | Template-method `Tool.invoke()` (validation/normalization public, `_invoke()` abstract) + message factories; AVOID god-class `tool_manager.py` (~1200 lines) |
| MCP (Phase 4) | `api/core/mcp/mcp_client.py`, `api/core/tools/mcp_tool/` | MCP = just another Tool family behind the same base class (our Rule 4) |
| Prompt engine | `api/core/prompt/{prompt_transform,advanced_prompt_transform,agent_history_prompt_transform}.py` | Prompt building as a transform fed a context object; token-budgeted history; `{{var}}` templates |
| Execution events | `dify-agent/src/dify_agent/protocol/schemas.py`; `runtime/event_sink.py`; `storage/redis_run_store.py` | Type-discriminated event union; terminal event = status transition; event-sink port with cursor replay (Last-Event-ID); delta coalescing |
| State/memory | `dify-agent/src/agenton/compositor/schemas.py`; `runtime/history.py` | Serializable session snapshots for resume; history as reserved concern |
| Workflow engine (Phase 5) | graphon `GraphEngine` + `GraphEngineLayer` middleware; `api/core/workflow/{workflow_entry,node_factory,node_runtime}.py`, `nodes/` | Protocol-injection seam (host capabilities wired into engine-generic nodes); persistence/observability/limits as engine layers; nodes return `NodeRunResult` OR yield events; versioned node classes |
| Persistence | `api/models/workflow.py`, `api/models/agent.py`, `api/models/model.py` | Snapshot definition into each run (immutable history + replay); step-execution rows with inputs/outputs/status/error/elapsed; AVOID LongText-JSON columns (we use typed JSONB) |
| API/streaming | `api/controllers/console/app/workflow.py`; `api/core/app/apps/base_app_generator.py::convert_to_event_stream`; `api/core/app/entities/queue_entities.py` | Thin controllers → services; typed event queue → per-event handlers → SSE framing in ONE place; durable event log + replay endpoint |
| Frontend builder (Phase 2) | `web/app/components/workflow/` (ReactFlow; NodeComponentMap keyed by BlockEnum; zustand slices + zundo); `hooks/use-nodes-sync-draft.ts` | Frontend registry mirrors backend node types; `_`-prefixed runtime-state convention stripped at save; draft-save with server hash for optimistic concurrency |
| Plugins (Phase 12) | `api/core/plugin/impl/*`, `plugin_service.py` | All external variety flows through one client boundary and surfaces via the same Tool/AIModel base abstractions |

**Adopt**: Protocol seams; engine layer/middleware; template-method tool invoke; discriminated-union events with terminal semantics; event-sink port with cursors; definition-snapshot-per-run; separate generate/stream; delta coalescing; SSE + Last-Event-ID.
**Simplify**: single in-process monolith (no plugin daemon / external agent service / Redis streams — Postgres + in-process bus); explicit registries (no import side effects); typed JSONB.
**Diverge**: we own the agent loop in our runtime (Dify's new backend delegates to pydantic-ai — we keep a hand-written loop behind a strategy protocol so ReAct/FC/custom stay first-class); no god-class facades; no dual config systems.
**Avoid (verified anti-patterns)**: `ToolManager` god class; triple-overloaded stream unions; 1100-line multi-protocol `node_runtime.py`; runtime state leaking into persisted graphs; boundaries leaking under pressure (poking `_execution_context`).

## Phase 1 design

### 1. Domain model (`domain/`, pure Pydantic, `extra="forbid"`)

- **agent.py**: `ModelRef{provider, model, base_url?, api_key_env}` (env-var *name*, never the key) · `ToolBinding{name, enabled, config}` · `MemoryConfig{enabled, max_messages=20, session_key}` · `AgentDefinition{id, name, description, model, system_prompt, user_prompt_template?, tools[], strategy: StrategyConfig{type: "function_calling"|"react", params}, memory, max_iterations=8 (1–32), temperature, output_schema?, created_at, updated_at}` · `AgentVersion{id, agent_id, version, snapshot: AgentDefinition, label, created_at}` (immutable, append-only)
- **execution.py**: `ExecutionStatus{RUNNING|SUCCEEDED|FAILED|CANCELLED|TIMED_OUT}` · `CancellationToken` (plain class wrapping `asyncio.Event`; `trigger(reason)`, `raise_if_triggered()`, derived child tokens shared by model adapter + tools) · `ExecutionContext{run_id, agent_id, agent_version_id, session_id?, user_id?, trace_id, metadata, cancel, deadline?, iteration, usage}` · `RunResult{run_id, status, final_message?, total_usage, iterations, error?, started_at, finished_at, event_cursor}`
- **message.py**: `Role{system|developer|user|assistant|tool}` · `ContentPart` union (TextPart first) · `ToolCall{id, name, arguments: dict}` · `Usage{input_tokens, output_tokens, extra}` · `Message{role, content: str|list[ContentPart], tool_calls?, tool_call_id?, name?, created_at}`
- **tools.py**: `ToolDescriptor{name, description, parameters (JSON Schema), annotations}` (annotations anticipate authorization flags) · `ToolResult{tool_call_id, tool_name, output, is_error, latency_ms, metadata}` · `ToolError{kind: validation|timeout|internal}` · `ToolContext{run_id, session_id?, user_id?, agent_id, variables, cancel, config}` (anticipates principal/secrets)
- **events.py**: shared envelope `{event_id, run_id, sequence (per-run, gapless, sink-assigned), created_at, type}`; discriminated union `ExecutionEvent`:
  `run.started` · `iteration.started` · `model.invocation.started{attempt}` · `text.delta` · `model.invocation.completed{usage, finish_reason}` · `tool.call.requested` · `tool.call.started` · `tool.call.completed{result}` · `tool.call.failed{error, kind}` · `iteration.completed` · terminal: `run.completed{final_message, total_usage, iterations}` / `run.failed{error, error_kind, total_usage}` / `run.cancelled{reason, total_usage}`
  **Invariants (tested)**: gapless sequence; exactly one terminal event per run (`EventSink.finalize()` terminal-only, once-only); no non-terminal event after a terminal one.

### 2. Ports (`ports/`, Protocols) and loop control

`ModelProvider{name, capabilities, generate(req, *, cancel) → ModelResponse, stream(req, *, cancel) → AsyncIterator[StreamDelta]}` · `ModelClient` (provider bound to ModelRef) · `ModelProviderFactory.resolve(ref)` · `Tool{descriptor, execute(args, ctx) → ToolResult}` · `ToolRegistry{register/get/descriptors}` (explicit; raises on duplicates) · `ToolRuntime.execute(call, ctx) → ToolResult` (validation/timeout/never-crash/events) · `AgentStrategy{name, step(ctx, messages, client, tools, sink) → StepOutcome}` where `StepOutcome = ToolCallsStep | FinishStep` · `StrategyRegistry.resolve(config)` · `EventSink{append → cursor, finalize → cursor}` · `EventStream{subscribe(run_id, last_cursor) → AsyncIterator, replay(run_id, after)}` · repositories: `AgentRepository{create, get, list, update_and_publish → AgentVersion, get_version, latest_version, delete}` · `ExecutionRepository{create_run, save_message, save_tool_execution, finish_run, get, list, list_events}` · `ConversationRepository{get_or_create, append_message, history}`.

**Central decision (ADR 0004) — orchestrator owns limits, strategy owns one step:**
`AgentRuntime.run(version, input, ctx)`: load history (memory) → `PromptEngine.build(PromptContext)` → emit `run.started` → loop while `iteration < max_iterations ∧ ¬cancelled ∧ within-budget ∧ within-deadline`: `strategy.step(...)` (strategy does NOT loop; it may stream `text.delta`/`model.*` via sink) → on `FinishStep` break; on `ToolCallsStep` execute each call via `ToolRuntime` (sequential in Phase 1), append tool messages → classify terminal: `run.completed | run.failed(error_kind: max_iterations|timeout|model|tool|output_schema) | run.cancelled` → `sink.finalize(...)` exactly once → persist RunResult + messages. **Blocking `/run` and streaming `/stream` call the SAME `AgentRuntime.run()`** — the SSE route subscribes to the sink while the run executes as an asyncio task.

DI: `api/deps.py` builds one `AppContainer` (settings → engine/session factory → repos → sink → registry → runtime); FastAPI deps hand out components; no globals.

### 3. Model layer (`models/`)

`ModelRequest{model, messages, tools?, temperature, max_tokens?, response_format?, stop?}` · `ModelResponse{message (assistant, may carry tool_calls), usage, finish_reason, model}` · `StreamDelta` union: `text_delta | tool_call_delta{index, id?, name?, arguments_fragment} | usage_delta | finish_delta` · `ModelCapabilities{streaming, function_calling, structured_output: none|json_mode|json_schema, parallel_tool_calls}`.

Error taxonomy: `ModelError` base → `Connection/Auth/RateLimit(retry_after)/BadRequest/Timeout/Aborted/Stream` errors, all carrying provider+model+status_code. Retry only RateLimit+Connection (max 2, exponential backoff, attempt on `model.invocation.started`).

Cancellation: HTTP call wrapped in an `asyncio.Task`; token event cancels it; `CancelledError` → `ModelAborted` → `run.cancelled`. Same token shared with `ToolContext`.

Structured output: json_schema → native `response_format`; json_mode (Ollama) → json_object + schema in system prompt (PromptEngine); none → prompt-only. Post-parse validation + **one repair retry** with the validation error appended as developer message; then `run.failed(error_kind="output_schema")`.

### 4. DB schema (PostgreSQL; typed Pydantic → JSONB, never raw strings)

- `agents` — mutable pointer row: id, name UNIQUE, description, current_version, timestamps
- `agent_versions` — **immutable append-only snapshots**: id, agent_id FK, version, snapshot JSONB (full AgentDefinition), label, UNIQUE(agent_id, version)
- `agent_executions` — id = run_id, agent_id, agent_version_id, session_id?, user_id?, trace_id, status (PG enum), input, output JSONB, error?, total_usage JSONB, iterations, timing, metadata JSONB; INDEX(agent_id, created_at DESC), INDEX(session_id)
- `conversations` — UNIQUE(agent_id, session_id), timestamps
- `messages` — conversation_id, execution_id, role (PG enum), content JSONB, tool_calls?, tool_call_id?, name?, sequence; UNIQUE(conversation_id, sequence)
- `tool_executions` — execution_id, tool_call_id, tool_name, arguments JSONB, result JSONB?, is_error, latency_ms
- `execution_events` — **cursor BIGSERIAL PK (global monotonic = SSE Last-Event-ID)**, execution_id, event_type, sequence (per-run gapless), payload JSONB (full event), UNIQUE(execution_id, sequence)

Snapshot-vs-reference: `agent_versions.snapshot` is the replay source; executions reference for filtering; edits never rewrite history (`update_and_publish` appends). Phase 1: `DELETE /agents/{id}` returns 409 if executions exist.

### 5. API surface (FastAPI, prefix `/v1`)

```
POST   /v1/agents                    201 (also publishes version 1)
GET    /v1/agents?limit&offset       list
GET    /v1/agents/{id}               definition + version index
PATCH  /v1/agents/{id}               update → auto-publish new AgentVersion
DELETE /v1/agents/{id}               204 / 409 if executions exist
GET    /v1/agents/{id}/versions/{n}  frozen snapshot

POST   /v1/agents/{id}/run           blocking: body {input, session_id?, user_id?, variables?, metadata?}
POST   /v1/agents/{id}/stream        SSE: id: <cursor>, event: <type>, data: <event JSON>;
                                    Last-Event-ID resume (replay after cursor, then live); terminal ends stream
POST   /v1/executions/{id}/cancel    idempotent; triggers the run's token → run.cancelled

GET    /v1/executions?agent_id&status&session_id
GET    /v1/executions/{id}           detail: run + messages + tool_executions
GET    /v1/executions/{id}/events?after   JSON replay (or SSE via Accept header)
GET    /v1/conversations/{agent_id}/{session_id}/messages
```

Error envelope `{"error": {kind, message, details}}` mapped from the domain taxonomy. SSE is a thin transport adapter (`api/sse.py`) — framing only.

### 6. CLI (`jarvis`, Typer + rich — reuses `AppContainer`, never re-implements logic)

`jarvis init` (migrate) · `serve` · `agent create --file agent.yaml | list | show NAME [-v N] | delete` · `run NAME "input" [--session S] [--stream] [--vars k=v]` · `executions list | show RUN [--events]` · `doctor [--ping-model]` · `version`. Proves the backend exists independently of any UI.

### 7. Builtin tools (Phase 1, small): `calculator`, `current_time`, `http_get` (allow-list enforcement; respx-tested). Echo/flaky/slow/bad-schema tools live in tests as mocks.

### 8. Test plan

`make test` (unit, no DB) / `make test-db` (compose up postgres → alembic → pytest `-m db`) / `make test-all`.

**MockModelProvider**: scripted turns (content | tool_calls | delta list | exception for failure injection), records every request for assertions, honors cancellation — deterministic full tool loop with zero network.

Unit (no DB): domain validation + event invariants (exactly-one-terminal, gapless) · prompt engine (templates, variables, history window, tool sections, schema-in-prompt fallback) · both strategies (FC incl. rate-limit retry; ReAct parsing, malformed-action recovery, max-iteration failure) · tool runtime (validation, never-crash, timeout, cancellation) · builtin tools · orchestrator with all mocks (happy path, max-iterations, timeout, cancel, budget, exactly-one terminal, memory window across runs, blocking-vs-streamed event-sequence equivalence) · model adapters (request/response translation, error mapping, cancellation, structured output + repair) · config.

Integration (Postgres): migrations up/down · repositories (version append-only, cursor monotonicity, conversation sequence) · agents CRUD API (auto-versioning, 409 delete) · blocking run (rows written) · SSE (framing, delta order, terminal close, **Last-Event-ID resume exactly-once**) · executions list/detail/replay/cancel · **e2e ReAct agent through the API with SSE** (mock provider + respx-mocked http_get).

### 9. Commit sequence (each independently green; 1–13 need no DB)

| # | Commit |
|---|---|
| 0.1 | `chore: scaffold repo (git init, pyproject, src layout, compose.yaml, Makefile, tooling)` |
| 0.2 | `docs: add architecture overview and ADRs 0001-0005` |
| 0.3 | `docs: add data-model, event-model, model-layer, api specs + dify reference map` |
| 1 | `feat(domain): add core domain models (agent, message, tools, execution, events)` |
| 2 | `feat(domain): enforce event envelope invariants (gapless, exactly-one-terminal)` |
| 3 | `feat(ports): define model, tool, strategy, event, repository protocols` |
| 4 | `feat(models): add model layer types, error taxonomy, capabilities` |
| 5 | `feat(models): add MockModelProvider with scripted turns and failure injection` |
| 6 | `feat(models): add OpenAI-compatible provider (generate + stream, base_url injection)` |
| 7 | `feat(tools): add tool base (template method), registry, and runtime` |
| 8 | `feat(tools): add builtin tools (calculator, current_time, http_get)` |
| 9 | `feat(prompt): add PromptEngine with templates, history window, tool sections` |
| 10 | `feat(strategies): add FunctionCallingStrategy` |
| 11 | `feat(strategies): add minimal ReActStrategy` |
| 12 | `feat(events): add in-process event sink with sequence assignment` |
| 13 | `feat(runtime): add AgentRuntime orchestrator (limits, budget, cancellation)` ← keystone |
| 14 | `feat(persistence): add SQLAlchemy models, alembic migration, repositories` |
| 15 | `feat(api): add FastAPI app, DI container, agents CRUD, run endpoint` |
| 16 | `feat(api): add SSE stream endpoint with Last-Event-ID resume` |
| 17 | `feat(api): add executions list/detail/events replay and cancel` |
| 18 | `feat(cli): add jarvis CLI` |
| 19 | `test(e2e): add ReAct end-to-end SSE test through the API` |
| 20 | `docs: add README with quickstart and agent.yaml example` |

### 10. Risks & deliberate deferrals

Risks: per-run sequence gaplessness must be assigned inside the sink (single asyncio task per run; DB unique constraint is the future authority) · Ollama structured-output is json-mode-only → the prompt-fallback + repair path is mandatory · SSE resume correctness hinges on cursor = durable `execution_events.cursor` (tested explicitly) · blocking vs streaming routes must produce identical event sequences (guarded by test).

Deferred with the seam each rides on: queues/distributed runs → `EventSink`/`EventStream` port · plugin strategies → `StrategyRegistry` · authorization/secrets/per-tool credentials → `ToolContext` + `ToolDescriptor.annotations` · MCP/RAG/sub-agents → new `Tool` implementations or `ContentPart` types · workflows → sibling executor reusing the same event model + persistence · multi-tenancy/auth → `ExecutionContext` caller-identity fields · tracing → `trace_id` plumbed and persisted (OTel later) · parallel tool calls → capability flag exists, execution sequential · jinja2 sandboxing → PromptEngine isolated, deferred until user-supplied templates are exposed.

## Verification (Phase 1 exit criteria)

1. `make test` — full unit suite green, no DB, no network, no LLM
2. `docker compose up -d postgres && make test-all` — migrations + repos + API + SSE + e2e green
3. `ruff check && mypy src` clean (strict on domain/ports)
4. CLI smoke: `jarvis agent create --file examples/research-agent.yaml && jarvis run research-agent "Explain Kafka consumer groups" --stream` (mock or local Ollama provider) — live event stream, then `jarvis executions show <run> --events` replays it
5. SSE smoke: `curl -N -X POST localhost:8000/v1/agents/{id}/stream` shows framed events; reconnect with `Last-Event-ID` receives missed events exactly once
6. Post-run DB check: execution + messages + tool_executions + events rows reconstruct the whole run

## Critical files

- `src/jarvis/domain/events.py` — envelope, union, exactly-one-terminal invariant (everything hangs off this)
- `src/jarvis/runtime/agent_runtime.py` — orchestrator; limits/budget/cancellation/terminal emission
- `src/jarvis/ports/model.py` — ModelProvider/ModelClient protocols; the adapter seam
- `src/jarvis/tools/runtime.py` — template-method tool execution
- `src/jarvis/persistence/models.py` — snapshot-vs-reference and cursor design