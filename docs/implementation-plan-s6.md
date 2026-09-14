# Implementation plan — S6: workflow engine (ADR 0015)

Stage: S6. Format follows `docs/implementation-plan-s4.md`. Every commit
below is independently green: `pytest tests/unit`, `ruff check src
tests` + format, `mypy src` — and per web commit `tsc --noEmit`,
eslint, `vitest`. Integration commits additionally pass
`uv run pytest tests/integration -q -m db` (local Postgres, no Docker).

## Verified seams (read in code, 2026-09-14)

All verified in this planning session — paths and names below are real:

- `domain/events.py:19` `_Event` envelope; the `ExecutionEvent` union;
  `TERMINAL_EVENT_TYPES`; `validate_event_sequence`.
- `runtime/agent_runtime.py` — `run()` (row create, in-try model D28 +
  tooling D38 resolution, `_loop`, `_after_loop`, terminal handlers);
  `resume()`; `_live_tokens`; `PauseOutcome`/`LoopOutcome`.
- `runtime/worker.py` — `_execute_claimed` branches on
  `message.resume`; `VersionLoader.get_version_by_id` structural
  protocol; sweeper rules (no events → requeue, else one terminal).
- `ports/queue.py:47` `RunQueueMessage` (agent_id/agent_version_id are
  plain strings); `ports/repository.py:22` `AgentRepo`
  (create/update_and_publish/get_version/latest_version shape — the
  WorkflowRepo mirror).
- `persistence/models.py` — `AgentRow`/`AgentVersionRow`/`
  AgentExecutionRow`/`RunQueueRow`/`ExecutionEventRow` (execution_id
  is a plain string; cursor is global BIGSERIAL).
- `persistence/migrations/versions/` — 0001..0007; next is 0008.
- `api/routes/agents.py` — `_queue_message`/`_queued_result`/
  `_await_segment`/`_queue_stream` (the helpers to extract, §5 of the
  ADR); `api/routes/capabilities.py:45` workflows flag (stage S6).
- `api/deps.py` `AppContainer.from_settings` (wiring order for the new
  repos + runtime + worker loaders).
- `prompt/engine.py:84` `render()` — plain `{{var}}` substitution (node
  input templates + condition values ride it; no Jinja).
- `web/src/app/routes.tsx:60` `/workflows` route exists, gated;
  capabilities-driven. Sections pattern: `web/src/sections/<name>/`.

## Confirmed decisions (ADR 0015 carries the contract)

### D41 — Workflow runs are agent_executions rows; the queue gains `kind`

One row per workflow run in the EXISTING `agent_executions` table
(`agent_id` column = workflow id, `agent_version_id` = workflow version
id, `metadata.kind = "workflow"`). Rejected: a parallel
`workflow_executions` table (duplicates the S1/S10 machinery the
roadmap told us to reuse). `RunQueueMessage.kind:
Literal["agent", "workflow"] = "agent"` — default keeps every existing
message byte-identical; the worker branches at its version-resolution
site only.

### D42 — Agent nodes pin agent versions at publish time

`update_and_publish` resolves `agent_id` → latest version and freezes
`agent_version_id` into the workflow snapshot. Replay fidelity is the
D1 argument one level up; a workflow version's meaning never changes
under it. The API lints stale pins (an agent has newer versions) as
warnings, never failures.

### D43 — `node_id` on the envelope; node-scoped events; no inner terminals

`_Event.node_id: str | None = None` (optional, backward compatible —
the ADR-0003 amendment is pre-declared in ADR 0015 §3). New event types
`node.started` / `node.completed`. A node's inner agent loop emits
through a `NodeSink` wrapper that stamps `node_id` and refuses terminal
appends; the workflow runtime alone finalizes. `node.failed` is not an
event type — the inner failure becomes the run's terminal
`run.failed`, error prefixed `node '<id>': ...`.

### D44 — Sequential walk, acyclic graphs, orchestrator-owned node cap

Graphs validate acyclic at create/update (422); execution walks from
the start node one node at a time (deterministic event order, trivially
replayable). `max_node_executions` (default 24) is enforced by the
workflow runtime — ADR 0004's rule transposed: the executor owns caps,
a node owns one step. Parallel fan-out, backward edges, and join nodes
are deferred (ADR 0015 §7) with the design note recorded.

## Design summary (details in ADR 0015)

- `domain/workflow.py` (new): `WorkflowDefinition`, `WorkflowNode`
  (`agent|tool|condition`), `WorkflowEdge`, `WorkflowVersion`;
  graph validators (unique node ids, edges reference nodes, one start,
  acyclic) raising pydantic-friendly validation errors for the API's
  422 mapping.
- `ports/repository.py`: NEW `WorkflowRepo` Protocol (mirror of
  `AgentRepo`: create/get/get_by_name/list/update_and_publish/
  get_version/latest_version/list_versions/delete/
  has_executions). No existing Protocol changes.
- `runtime/workflow_runtime.py` (new): `WorkflowRuntime.run()` —
  never raises, one terminal, owns the row lifecycle like
  `AgentRuntime.run`; resolves graph + agent versions + tool-node MCP
  bindings eagerly in-try (D28 pattern, fifth application); walks
  nodes; `NodeSink` stamps node_id.
- `runtime/agent_runtime.py` (refactor): extract
  `_run_segment(agent, input, ctx, sink) -> LoopOutcome` — client +
  tooling resolution, prompt build, loop, message persistence, NO
  terminal, NO row writes. `run()` wraps it with the existing terminal
  and row handling. THE risk concentrator — gated by byte-identical
  replay tests before anything rides it.
- `runtime/worker.py`: `WorkflowVersionLoader` structural protocol +
  the `message.kind` branch; `SegmentRunner` structural protocol the
  workflow runtime consumes (fakes in unit tests).
- Migration 0008: `workflows` + `workflow_versions` (mirror of
  agents tables, tenancy included: tenant_id NULL = shared, partial
  unique indexes for name per tenant + shared — the 0007/McpServerRow
  pattern).
- API: `api/routes/workflows.py` (new) + run/stream helpers extracted
  from agents.py into `api/routes/_run_routes.py` (private shared
  module, `kind` parameter); `capabilities` workflows flips with a
  derived detail (`node_types`, count).
- Web: `@xyflow/react` canvas section, node registry mirroring
  backend types, draft-save with server hash, `_`-prefixed state
  stripped at save, timeline node grouping.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| `node_id` on envelope + 2 event types | `domain/events.py` | ADR-0003 amendment — pre-declared ADR 0015 §3 |
| `RunQueueMessage.kind` | `ports/queue.py` | ports/ change — pre-declared ADR 0015 §4 |
| `WorkflowRepo` (NEW Protocol) | `ports/repository.py` | addition only, no existing Protocol changes |
| `_run_segment` extraction | `runtime/agent_runtime.py` | internal, gated by byte-identical replay |
| worker kind branch | `runtime/worker.py` | structural protocols added; claim/lease/sweep untouched |
| migration 0008 | `persistence/` | new tables only |
| shared run helpers | `api/routes/` | extraction; agent routes byte-identical |

No changes to: error shape, event types' existing shapes, tenancy
rules, secrets, `EventSink`/`EventStream`/`RunQueue` Protocols,
strategy port, model port.

## Commit sequence (each independently green)

1. **`docs(adr)` — this plan + ADR 0015 + D41–D44 in decisions.md.**
   No code. (The stage record commit, the S4 pattern.)
2. **`feat(domain)` — workflow domain + graph validation.**
   `domain/workflow.py`, validators, unit tests: valid graphs build,
   cycle/duplicate-id/dangling-edge/missing-start raise with
   field-addressable errors; `WorkflowVersion` snapshot round-trip.
   Node config validation per type (agent node requires agent_id;
   condition routes reference in-graph nodes — validated at the
   definition level, publish adds the pin).
3. **`feat(persistence)` — WorkflowRepo port + SQL adapter +
   migration 0008.** Mirror of the agents pair; tenancy scoping +
   partial unique indexes (0007 pattern); `sa.Enum` create_type gotcha
   N/A (no new enums); integration tests for CRUD + publish +
   tenant-scoping 404s.
4. **`refactor(runtime)` — extract `_run_segment` from
   AgentRuntime.run().** THE gate commit: the full existing
   agent-runtime unit suite green + NEW sequence-equality tests
   (fresh run, structured-output repair path, tool loop, pause paths
   — event lists asserted identical pre/post refactor via golden
   sequences captured in fixtures). No behavior change; `run()` is a
   thin wrapper. If a sequence diverges, fix before proceeding —
   nothing downstream may ride an unverified extraction.
5. **`feat(events)` — `node_id` envelope field + `node.started`/
   `node.completed` types + NodeSink.** Union members, replay
   round-trip (`node_id: None` on old events), NodeSink unit tests
   (stamps every event, refuses terminal appends with
   EventSequenceError). Web `schema.d.ts` untouched this commit
   (regen comes with the API commit).
6. **`feat(runtime)` — WorkflowRuntime + worker kind branch.**
   `runtime/workflow_runtime.py`; `SegmentRunner`/`
   WorkflowVersionLoader` protocols; `RunQueueMessage.kind` +
   worker branch; unit tests with fake segment runners: linear
   2-agent chain, condition routing both ways, tool node (fake
   registry), node cap terminal (`max_iterations`), inner-failure
   terminal naming the node, cancel/deadline mid-walk, never-raises
   (resolution failure → terminal `model`), usage accumulation +
   token cap. Existing worker tests green (default kind byte-identical
   message shape).
7. **`feat(api)` — workflow routes + shared run helpers + capabilities
   flip + gen-api regen.** Routes mirror agents (CRUD/versions/
   publish lints: stale pins, ignored memory, unreachable nodes);
   run/stream via the extracted helpers; executions name resolution
   for workflow rows; `capabilities.workflows` derived detail.
   Integration: e2e workflow run through /run + /stream with the mock
   provider (function_calling, D19 gotcha), pause-inside-node (an
   approval-gated tool node) → resume → walk continues (the S10
   composition the ADR §7 calls out), foreign-tenant 404.
8. **`feat(web)` — Workflows section: canvas + run console.**
   `@xyflow/react` dep; node palette/config forms (agent picker with
   pin shown, tool picker from capabilities, condition routes);
   draft-save with server hash; `_`-strip at save; workflow run console
   (RunConsole reuse) + EventTimeline `node_id` grouping; executions
   detail groups workflow runs by node. vitest for: canvas render +
   draft round-trip (strip check), node forms validation, timeline
   grouping reducer, capability-gated coming-soon → live flip.
9. **`docs(s6)` — closure.** README workflows section, roadmap
   shipped note, `docs/walkthrough-s6.md` (live session script),
   CLAUDE.md gotchas from the build. ONLY after Harshit's
   manual-testing session (per working agreement).

## Risks & deliberate deferrals

- **`_run_segment` extraction (commit 4) is the one real risk.** The
  run path concentrates row lifecycle, terminal handling, S10 resume
  entries, and S4 tooling closure. Mitigation: golden sequence
  equality tests across every path before any consumer exists; the
  commit is pure refactor (ruff/mypy clean, no new behavior).
- **A condition node's "most recent upstream output"** is the output
  of the node the walk came FROM (the single predecessor edge taken),
  not a merge of all predecessors — sequential walks make this
  unambiguous; recorded in the ADR and unit-tested.
- **Mixed fleet** (old worker, new API): a workflow message claimed by
  an old worker fails version resolution → the existing honest
  terminal path; deployment note = restart workers with the API (the
  standing single-restart discipline).
- **Deferred (ADR 0015 §7):** parallel branches, backward edges/loops,
  nested/sub-workflows, workflow-level memory, Jinja expressions,
  human-input nodes, per-node credentials. Each recorded with the
  reopen note, not silently dropped.

## Verification (S6 exit criteria — roadmap §S6 acceptance)

- A two-node workflow runs through `/run` and `/stream` with SSE;
  events carry `node_id`; the run row is a first-class execution
  (list/detail/cancel/resume all work on it).
- A failed node yields `run.failed` naming the node with the inner
  error kind; the pre-node events replay identically.
- Cursor/resume tests pass verbatim (SSE Last-Event-ID across a
  workflow run; a pause-inside-node resumes and completes).
- Agent runs' event sequences byte-identical pre/post refactor
  (commit-4 golden tests, kept as permanent regression).
- Capabilities flips `workflows` to enabled with a derived detail; the
  web section lights up from it; vitest/tsc/eslint green.
- Final gates: unit suite (projected ~430+), integration (~135+),
  ruff, mypy, tsc, eslint, vitest.

## Walkthrough draft (for `docs/walkthrough-s6.md`, live session)

Commit 9 seeds the doc; the live session script will cover: create +
publish a 3-node workflow (agent → condition → agent/tool branch) via
API + UI canvas; run it live on `gemma-cloud-agent` nodes; watch node
grouping in the timeline; the pause-inside-node resume; a deleted
agent version → publish lint + honest terminal failure; the executions
listing showing workflow runs with names; anonymous/tenant behavior
mirrors agents.