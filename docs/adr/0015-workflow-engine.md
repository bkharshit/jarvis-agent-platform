# ADR 0015 — Workflow engine (S6): a sibling executor over the same event model

Status: accepted (planning ADR — no S6 code exists at time of writing)
Date: 2026-09-14
Supersedes: none (implements the roadmap S6 sketch; the superseded S5
frontend-builder stage is folded into this stage's UI enablement)

## Context

Everything through S4 executes ONE agent loop per run. The roadmap's S6
sketch (verified against code before this ADR) calls for DAG/graph runs
beyond the single-agent loop, riding on the deliberate Phase 1 decision
that workflows are a *sibling executor* reusing the event model,
persistence, and limits — not a new paradigm.

Verified seams this design rides on (paths checked 2026-09-14):

- `domain/events.py` — discriminated `ExecutionEvent` union with a
  frozen shared envelope (ADR 0003); `TERMINAL_EVENT_TYPES`,
  `PAUSE_EVENT_TYPE`, `validate_event_sequence` (gapless per-run
  sequence, exactly one terminal, nothing after it).
- `runtime/agent_runtime.py` — `AgentRuntime.run()` owns the loop,
  resolves model (D28) and MCP tooling (D38) inside its try, emits
  through `InProcessEventSink`, finalizes exactly once, never raises.
- `runtime/worker.py` — `Worker._execute_claimed` claims a
  `RunQueueMessage`, resolves `AgentVersion` through the
  `VersionLoader.get_version_by_id` structural protocol, and calls
  `runtime.run`; the sweeper's requeue rule is "no events → requeue,
  else exactly one terminal".
- `ports/queue.py` — `RunQueueMessage` is the trust boundary: run_id,
  agent_id, agent_version_id, input, tenant/principal, deadline.
- `persistence/models.py` — `agents`/`agent_versions` (pointer row +
  immutable JSONB snapshot, D1), `agent_executions` (run rows keyed by
  run_id, `agent_version_id` a plain string), `execution_events`
  (global BIGSERIAL cursor + per-run sequence — keyed by execution_id
  only, so any run id can stream), `run_queue` (payload JSONB).
- `api/routes/agents.py` — run/stream enqueue then subscribe through
  `PgEventStream`; `_await_segment`/`_queue_stream` end on terminal or
  pause (S10).
- `api/routes/capabilities.py` — the `workflows` section flag ships
  disabled with `stage: "S6"`; the UI renders from this payload.
- `prompt/engine.py` — `{{var}}` template substitution, reusable for
  node input templates.
- Dify reference (`docs/reference/dify-map.md`): frontend registry
  mirrors backend node types; `_`-prefixed runtime state stripped at
  save; draft-save with server hash for optimistic concurrency. Dify's
  engine-middleware layering is deliberately NOT copied — our limits,
  persistence, and events are already runtime-owned.

## Decision

### 1. WorkflowDefinition and versioning mirror agents exactly

`domain/workflow.py` (new, pure Pydantic):

- `WorkflowDefinition`: `id`, `name` (unique), `description`,
  `nodes: list[WorkflowNode]`, `edges: list[WorkflowEdge]`,
  `start_node_id`, `max_node_executions: int` (default 24, ge 1 le 128),
  timestamps.
- `WorkflowNode`: `id` (slug, unique within the workflow),
  `type: Literal["agent", "tool", "condition"]` (v1), `config` (typed
  per node type, below).
- `WorkflowEdge`: `from_node`, `to_node`, optional `label` (the
  condition-route name).
- `WorkflowVersion`: immutable append-only JSONB snapshot, same shape
  as `AgentVersion` — `update_and_publish` appends and repoints
  `workflows.current_version`. History is never rewritten (D1).

Validation at the domain edge (create/update boundary, 422): unique
node ids, every edge references existing nodes, exactly one start,
`start_node_id` in nodes, and **acyclicity** (topological check). An
unreachable node is allowed (drafts) but named in a lint list the API
returns as a warning field, not an error.

**Agent nodes pin agent versions at PUBLISH time.** An agent node's
config carries `agent_id` + `agent_version_id` resolved at
`update_and_publish` (latest at publish); the snapshot freezes it. A
workflow run therefore replays the exact agent content it ran with —
the same fidelity argument as D1, one level up. Rejected: resolving
"latest" at run time (a republished agent silently changes what a
pinned workflow version means; the D37 name-join pattern does not apply
— MCP names are registry join keys, agent versions are content).
Consequence, accepted: publishing an agent does NOT auto-republish
workflows; the API's workflow detail surfaces stale-pin facts (the
pinned version still exists — versions are immutable — so this is a
lint, never a failure).

### 2. Node configs (v1)

- **Agent node** `{agent_id, agent_version_id, input_template}`: runs
  one agent with `{{...}}`-templated input. Template variables: the
  workflow input (`input`), upstream node outputs (`node.<id>`), and
  run `variables`. Memory: node agents run **memory-less** in v1 (no
  conversation row is created for a node execution) — node agents that
  enable memory have it ignored, with a publish-time lint warning.
- **Tool node** `{binding: ToolBinding-like {name, config}}`:
  executes one bound tool (builtin or `mcp__server__tool`) with
  templated arguments. MCP resolution is eager per workflow segment —
  the D38 pattern, fourth application: resolution failure is the
  workflow-level persisted terminal `run.failed` with
  `error_kind="tool"` naming the server, before any node runs.
- **Condition node** `{routes: [{when: {operator, value}, to_node}],
  else_node}`: `operator` is a closed literal set
  (`contains`, `equals`, `regex`, `not_empty`), evaluated against the
  most recent upstream node's output text; first match wins, `else`
  required. No model call, no Jinja — the prompt engine's plain
  `{{var}}` substitution is the only templating in the platform.

Parallel branches are deliberately OUT of v1 (see §7).

### 3. The event envelope gains one optional field; new event types are added

`_Event` (the frozen envelope, ADR 0003) gains:

    node_id: str | None = None

This is an envelope change and is pre-declared here per the CLAUDE.md
rule 7. It is backward compatible in both directions: old persisted
events replay as `node_id: None`; `validate_event_sequence`,
`TERMINAL_EVENT_TYPES`, cursor semantics, and every consumer keyed on
`type` are untouched.

New event *types* (added to the union):

- `node.started` `{node_id, node_type}`
- `node.completed` `{node_id, node_type, output, is_error: bool = False}`
- `node.failed` is NOT an event type — a node's inner failure becomes
  the workflow run's terminal `run.failed` naming the node (§5): one
  failure, one terminal, no partial-node ambiguity.

All events a node emits (its inner `iteration.*`, `text.delta`,
`tool.call.*`, `model.*`) carry the executing node's `node_id`,
stamped by a sink wrapper (§5) — the event log stays one gapless
sequence per workflow run and the timeline shows node grouping for
free. The inner agent loop emits NO terminal event inside a node:
`node.completed` carries the agent's final message instead.

### 4. Workflow runs ARE agent_executions rows; the queue carries a kind

- A workflow run is one row in the existing `agent_executions` table:
  `agent_id` column carries the workflow id, `agent_version_id` the
  workflow version id, `metadata.kind = "workflow"`. No new run-shaped
  table, no duplicated resume/cancel/sweeper/event machinery — the
  whole S1/S10 surface (queue, leases, requeue, pause-reaper,
  cross-process cancel, `PgEventStream`, SSE Last-Event-ID resume)
  applies to workflow runs unmodified. Rejected: a separate
  `workflow_executions` table (duplicates every path the roadmap told
  us not to rebuild).
- `ports/queue.py` — `RunQueueMessage` gains
  `kind: Literal["agent", "workflow"] = "agent"`. Default keeps every
  existing message byte-identical; workers ignore messages of an
  unknown future kind. This is the one ports/ change and it is
  pre-declared here.
- `Worker` gains a second structural loader (`WorkflowVersionLoader:
  get_workflow_version_by_id`) and branches on `message.kind`:
  agent messages resolve through the agent loader exactly as today;
  workflow messages resolve the workflow version snapshot and call
  `WorkflowRuntime.run` instead of `AgentRuntime.run`. The claim path,
  heartbeat, stale-resume guard, and ack/sweep logic are shared
  verbatim — the branch is two lines at the resolve site.
- `/v1/executions` shows workflow runs; the API resolves display names
  from both repos (an execution whose agent_id is a workflow id shows
  the workflow name). Executions detail/replay/cancel/resume work with
  zero route changes because they key on run_id.

### 5. WorkflowRuntime — a sibling executor, not a loop strategy

`runtime/workflow_runtime.py` (new). Public surface mirrors
AgentRuntime: `run(version, input, ctx, sink) -> RunResult`,
**never raises**, finalizes exactly one terminal event, owns the run
row lifecycle exactly as `AgentRuntime.run` does (RUNNING row at start,
`finish_run` at terminal).

Execution semantics (v1 — sequential, deterministic):

1. Inside the run's try (the D28 pattern, fifth application): resolve
   the graph from the snapshot; eager-resolve tool-node MCP bindings
   (D38 fourth application); resolve each agent node's pinned
   `AgentVersion` through the version loader. Any resolution failure
   is the persisted terminal `run.failed` (`error_kind="model"` for a
   missing agent version, `"tool"` for MCP) — before a single token.
2. Walk from `start_node_id`. For each node: `ctx.check_limits()`
   (deadline/cancel are the run's own), enforce
   `max_node_executions` (the orchestrator-owns-limits rule, ADR 0004:
   a runaway condition loop terminates `run.failed`
   `error_kind="max_iterations"` naming the cap).
3. Emit `node.started`; execute the node; emit `node.completed` with
   the node's output; follow the matching outgoing edge (condition
   nodes pick one; dead ends end the walk). A node with no outgoing
   edge after completing ends the workflow with that output.
4. Terminal: `run.completed` with the last executed node's output as
   `final_message`, total usage summed across nodes, iterations = node
   count. Cancellation/deadline → `run.cancelled` exactly as agents.
   A node's inner failure → terminal `run.failed`, `error_kind` from
   the inner outcome (`model`/`tool`/`strategy`/...), error string
   prefixed `node '<id>': ...`.

**Node execution reuses AgentRuntime's loop, not a copy.**
`AgentRuntime` is refactored to expose an internal
`_run_segment(agent, input, ctx, sink, node_id) -> LoopOutcome`-shaped
method: resolve client + tooling (D28/D38 inside the segment's try),
build the prompt, run the existing `_loop`, persist messages under the
workflow run id, and RETURN the outcome WITHOUT emitting a terminal
event or touching the run row. `AgentRuntime.run()` becomes a thin
wrapper over `_run_segment` + its own terminal/row handling —
byte-identical event sequences for plain agent runs are the
regression gate. `WorkflowRuntime` calls `_run_segment` through a
narrow structural protocol (`SegmentRunner`), so the workflow runtime
unit-tests with a fake runner and the agent path stays independently
testable.

The sink wrapper: a `NodeSink` implements the `EventSink` protocol by
delegating to the workflow run's sink, stamping `node_id` on every
event and *rejecting* terminal appends (the segment runner returns
outcomes; a terminal from inside a node is a bug and raises
`EventSequenceError` — caught by the workflow runtime's own
never-raise handlers).

Tool nodes execute through the unchanged `ToolRuntime` with the same
wrapper (their `tool.call.*` events carry `node_id`; the
`tool_executions` row lands under the workflow run id — visible in the
executions detail exactly like an agent's).

Usage accounting: `ctx.usage` accumulates across nodes (the inner loops
already write into a shared `ExecutionContext` usage); the token budget
cap (`RunLimits.max_total_tokens`) therefore spans the whole workflow
run — limits are owned by the orchestrator, not per node.

LLM trace (ADR 0014): unchanged — node agent resolution goes through
the same factory, so `TracedModelClient` wraps each node's client
under the workflow run id.

Human-in-the-loop inside a workflow: OUT of v1 (see §7) — but the
design keeps the door open: a node agent's approval pause would be the
existing `run.awaiting_input` segment end with the node's `node_id` on
the frame; resume re-resolves and continues the walk. The pause-reaper,
blocking resume, and PauseCard all work unmodified when this lands.

### 6. API surface and UI enablement

- `/v1/workflows` (new route module, mirrors agents): CRUD +
  `update_and_publish` + versions listing; create/update validate the
  graph (§1) and lint (stale pins, ignored memory, unreachable nodes)
  in the response. Tenancy identical to agents (owner tenant stamps,
  NULL = shared; foreign tenant 404, D29).
- `POST /v1/workflows/{id}/run` + `/stream`: extracted shared helpers
  from the agents routes (`_queue_message`, `_await_segment`,
  `_queue_stream`) move to a shared module with a `kind` parameter;
  agent routes are byte-identical in behavior. SSE framing, pause
  semantics, and resume are the existing ones.
- `GET /v1/capabilities`: `workflows` flips to
  `enabled: true` with a derived detail (`node_types` list, workflow
  count) — the fact flips only when the routes are real.
- UI — **the Workflows section** (the superseded S5 core):
  - React Flow canvas (`@xyflow/react`): node registry mirrors the
    backend node types (agent/tool/condition), palette + drag/drop +
    edge drawing; node config forms (agent picker with version pin
    shown, tool picker over capabilities, condition route editor).
  - Draft-save with server hash for optimistic concurrency; `_`-prefixed
    runtime state (positions, selection) stripped at save (Dify
    lessons, verified in dify-map).
  - Workflow run console reuses the existing SSE RunConsole; the
    event timeline groups by `node_id`; the executions detail page
    renders workflow runs with node grouping.
  - Capability-gated as always: the section lights up from
    capabilities, never hardcoded.

### 7. Deliberately out of v1 (recorded, not silently dropped)

- **Parallel branches / fan-out.** Sequential walk keeps the event
  order deterministic and the replay contract trivially true; Dify's
  concurrency surfaced ordering bugs our event invariants would
  inherit. Design note recorded for the follow-up: parallel nodes need
  per-branch sub-contexts with merged usage and an explicit join node;
  the `node_id` stamping and NodeSink design below anticipate it.
- **Loops (backward edges) in execution.** The graph is validated
  acyclic; `max_node_executions` exists so a future cycles feature
  inherits its cap. Condition nodes express routing, not iteration.
- **Human-in-the-loop nodes.** §5 records the open door; v1 agent-node
  runs with approval-gated tools still pause — but v1 workflow runs
  REFUSE to publish an agent node whose pinned agent binds
  approval-gated tools? No — simpler and honest: v1 allows it and the
  pause path is exercised exactly as S10 built it (it composes
  naturally: the pause is a segment end carrying node_id; resume
  continues the walk). This is verified by an integration test, not
  blocked.
- **Nested workflow nodes, sub-workflows, human-input nodes,
  Jinja/Python expression nodes.** Deferred with S14 (multi-agent) in
  mind: an "agent-as-tool" composes with v1 already (a tool node can
  call any bound tool; S14 adds the agent-as-tool family).
- **A workflow-level memory/conversation.** Workflow runs are
  stateless chains in v1; the executions transcript (messages +
  events, already persisted per run id) is the record.

## What stays frozen

- The event envelope's existing fields and every current event type's
  shape (only the optional `node_id` is added, ADR-0003-amended here).
- Terminal semantics: exactly one terminal per run, `run.awaiting_input`
  a segment end, nothing after terminal — workflow runs inherit all of
  it unchanged.
- `ExecutionEvent` persistence (JSONB over the existing
  `execution_events`), the global cursor, SSE framing, Last-Event-ID
  resume.
- Agent runs' behavior and event sequences (the `_run_segment` refactor
  is gated by byte-identical replay tests).
- The error envelope, tenancy rules (D29), secret rules (ADR 0005/0006
  — no new credential surface in S6), the queue's claim/lease/sweep
  contract (only the `kind` field is added).
- `ports/`: the ONLY change is `RunQueueMessage.kind` (pre-declared
  §4). Everything else rides existing Protocols: `EventSink`,
  `EventStream`, `RunQueue`, `AgentRepo`-shaped access via new
  `WorkflowRepo` (added to `ports/repository.py` — a new Protocol, not
  a change to an existing one), and the structural
  `VersionLoader`/`SegmentRunner` protocols internal to the runtime.

## Consequences

- One runtime to audit for never-raises/one-terminal: the workflow
  runtime wraps the SAME failure taxonomy (`model`, `tool`,
  `strategy`, `max_iterations`, `timeout`) — no new `error_kind`
  values.
- The executions surface doubles as the workflow history without a
  new section: the UI grouping work is frontend-only.
- The `_run_segment` refactor is the risk concentrator; the plan gates
  it with the existing full agent-runtime unit suite plus
  event-sequence equality assertions before any workflow code rides
  it.
- The queue `kind` field means a mixed fleet (old worker, new API)
  would claim a workflow message and fail version resolution; the
  honest failure is the existing "version not found" terminal path,
  and the deployment note is "restart workers with the API" (the same
  single-restart discipline as every prior stage).
- Frontend gains its first canvas dependency (`@xyflow/react`); the
  canvas state lives in a zustand slice like the agent editor, and the
  generated OpenAPI client remains the schema authority (make gen-api
  re-run is a plan step).