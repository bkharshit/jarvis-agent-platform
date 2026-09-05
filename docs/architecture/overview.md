# Architecture Overview

JARVIS is a modular monolith built **agent-runtime-first**. Everything else
in the roadmap — tools platform, MCP, workflows, RAG, frontend, plugins,
multi-agent — rides on interfaces the runtime establishes now.

## Layers (dependency rule, lint-enforced)

```
            ┌─────────────────────────────────────────────┐
 api/ cli/  │  delivery: FastAPI routes, SSE framing,     │
            │  Typer commands (thin; no logic)            │
            ├─────────────────────────────────────────────┤
 runtime/   │  orchestration: AgentRuntime (loop, limits, │
 events/    │  cancellation, terminal emission)           │
 strategies │  one model call per step                    │
 models/    │  provider adapters (openai-compatible, mock)│
 tools/     │  tool base/registry/runtime, builtins        │
 prompt/    │  PromptEngine                               │
 persistence│ repositories (SQLAlchemy → Postgres JSONB)  │
            ├─────────────────────────────────────────────┤
 ports/     │  Protocols ONLY — the seams                 │
 domain/    │  pure Pydantic models, no IO                │
            └─────────────────────────────────────────────┘
```

**Rule**: `domain/` and `ports/` import only pydantic/stdlib. Everything
else depends inward. Delivery layers are swappable by construction (CLI and
HTTP already share `AppContainer`).

## Stable interfaces (Phase 1 contracts)

| Interface | Port | Notes |
|---|---|---|
| Model access | `ports/model.py` — `ModelProvider`, `ModelClient`, `ModelProviderFactory` | generate/stream separated; capabilities flags; `list_models` (ADR 0005, 0007) |
| Tools | `ports/tools.py` — `Tool`, `ToolRegistry`, `ToolRuntime` | template-method invoke; MCP later = another Tool family |
| Strategies | `ports/strategy.py` — `AgentStrategy`, `StepOutcome`, `StrategyRegistry` | one step per call; orchestrator owns the loop (ADR 0004) |
| Events | `ports/events.py` — `EventSink`, `EventStream` | cursor replay; exactly-one-terminal (ADR 0003); `subscribe` yields `(cursor, event)` pairs (ADR 0008 amendment) |
| Persistence | `ports/repository.py` — `AgentRepo`, `ExecutionRepo`, `ConversationRepo` | Pydantic-over-JSONB (ADR 0002) |
| Run queue | `ports/queue.py` — `RunQueue`, `RunQueueMessage` | Postgres `SKIP LOCKED` adapter (S1, ADR 0008); Redis later behind the same protocol |

## What a run looks like

`AgentRuntime.run(version, input, ctx)`:

1. Load history (memory, if enabled) → `PromptEngine.build(PromptContext)`
2. Emit `run.started`
3. Loop while `iteration < max_iterations ∧ ¬cancelled ∧ within-budget ∧
   within-deadline`:
   - `strategy.step(...)` — one model invocation; may stream deltas via sink
   - `FinishStep` → break; `ToolCallsStep` → execute each call via
     `ToolRuntime` (sequential), append tool messages
4. Classify terminal: `run.completed | run.failed | run.cancelled` →
   `sink.finalize()` exactly once
5. Persist `RunResult` + messages + tool executions

Blocking `/run` and streaming `/stream` execute the **same** method; since
S1 the routes enqueue (row + queue message in one transaction) and a
`Worker` runs that method, streaming events back out of the database via
`PgEventStream` (LISTEN/NOTIFY wake-ups over the DB tail — the DB is the
sole source of truth, NOTIFY only wakes).

## Phase roadmap (summary)

- **Phase 0-1 (this repo state)**: runtime, tools, strategies, persistence,
  API + SSE, CLI — fully testable with the mock provider, no LLM needed.
- **F1 (next, starts immediately)**: the **product shell** — full
  Dify-inspired IA (Agents, Workflows, Tools, Models, Knowledge,
  Executions, Evaluations, Observability, Plugins, Triggers, Settings).
  Agents / Executions / Conversations are live against the Phase 1 API;
  every unimplemented section renders disabled/coming-soon, gated by
  `GET /v1/capabilities` (`docs/architecture/frontend-architecture.md`).
- **Phase 2+ backend stages (roadmap S1–S14)**: each stage lands its
  backend capability *and* flips on its UI section (S2 → Settings, S4 →
  MCP in Tools, S6 → the workflow canvas, S8 → Knowledge, …). The frontend
  no longer waits for the backend to finish (decision 1.6).
- **MCP**: just another Tool family behind `Tool`.
- **Workflow engine**: sibling executor reusing the event model,
  persistence, and limits.
- **Later**: plugins/marketplace, multi-agent, triggers.

Deliberately **not** in Phase 1: Redis (in-process event bus; the `EventSink`
port anticipates queues), plugins, multi-tenancy, RAG, workflows,
multi-agent.