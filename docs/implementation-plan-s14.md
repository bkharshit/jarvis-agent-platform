# Implementation plan — S14: multi-agent — sub-agents and handoffs (ADR 0018)

Stage: S14. Format follows `docs/implementation-plan-s11.md`. Every
commit below is independently green: `pytest tests/unit`, `ruff check
src tests` + format, `mypy src` — and per web commit `tsc --noEmit`,
eslint, `vitest`. Integration commits additionally pass
`uv run pytest tests/integration -q -m db` (local Postgres, no Docker).

Scope (ADR 0018): the `sub__`/`handoff__` dynamic tool family with
publish-time version pinning + cycle detection; child runs as ordinary
durable queue runs with a blocking, bounded stream wait; the additive
`parent_run_id` linkage (envelope + column + ports); budget roll-up
and cancellation fan-out; non-interactive children (auto-declined
approvals, auto-answered asks); handoff = successor run carrying the
rendered transcript; executions parent/child links; capabilities flip;
web agent-editor picker + execution-detail links. Handoffs are IN v1
(Harshit's scope call, 2026-09-15).

## Verified seams (read in code, 2026-09-15)

- `runtime/agent_runtime.py` — `_resolve_tooling` (`:1147-1156`, the
  D38 site the provider composes into); loop checkpoints `:404-417`;
  approval gate + decline branch `:528-575` (refusal copy `:559-565`);
  pause sites `:502-510` (AskHumanStep) and `:534` (approvals);
  tool execution `:586-598`; `LoopOutcome` `:79-86` + `_after_loop`
  `:297` (single outcome→terminal dispatch); `_result` `:1101-1130`.
- `tools/runtime.py:44-49` — timeout precedence; **`None` falls
  through to the 30s default** (the trap §5's sentinel closes).
  `tools/mcp/provider.py:106-161` — the dynamic-family template;
  `api/deps.py:100-111` — the registration loop.
- `events/pg_notify.py:141-195` — `subscribe` on a just-enqueued row;
  `end_on_pause_status`; 1s fallback poll. `events/bus.py:100-106`
  (`_append` stamps sequence in place — the parent_run_id choke
  point); `:144-181` (NodeSink, the additive-field precedent).
- `runtime/worker.py` — heartbeat `:367-383` (independent task, pops
  `pending_cancel`); sweeper `:387-421` (claimed-lease expiry only —
  never pending rows: the bound the tool must own); `_context`
  `:352-365`; sink construction `:181`. `worker.py:245` resolves by
  `agent_version_id` — pinned children need NO worker change.
- `api/routes/_run_routes.py:39-86` — `queue_message` (run_id at
  enqueue, metadata/kind stamps) / `queued_result`;
  `api/evaluation_service.py:58-101` — the child-enqueue composition;
  `persistence/repositories.py:691-700` `create_queued_run` (one tx).
- `api/routes/agents.py:164-186` — `update_agent` snapshots verbatim;
  `api/routes/workflows.py:134-164` `_pin_agent_versions` — the pin
  pattern to transpose; `domain/workflow.py:193-213` `validate_acyclic`
  — the cycle-walk precedent over immutable snapshots.
- `domain/execution.py:82-105` (ExecutionContext; metadata free-form),
  `:114-135` (RunResult + metadata); constructed in exactly
  `worker._context` + `cli/main.py:373` — threading is closed.
- `persistence/models.py:152-199` (`agent_executions` — no parent
  column today), `:396-426` (`messages` dual-keyed; transcripts by
  `execution_id`), `:369-393` (conversations unique on `(agent_id,
  session_id)` — cross-agent handoff needs explicit transfer);
  `repositories.py:887-917` `save_message`/`list_messages`;
  `agent_runtime.py:930-979` `_rebuild_messages`.
- `domain/events.py:167-171` — `RunCompleted` carries `total_usage`
  (the race-free usage source); `:19-34` the envelope (`extra=forbid`).
- `persistence/migrations/versions/` — 0001..0011; next is 0012.
- `api/routes/capabilities.py` — `_SECTION_FLAGS` + derived-detail
  pattern; web `sectionRegistry.ts`/`detail.ts` accessor pattern;
  `web/src/sections/agents/AgentEditor.tsx:495-569` (the MCP picker to
  mirror); `web/src/sections/executions/ExecutionDetail.tsx`
  (summary block seam for children links); `runConsole.ts:11` WireEvent
  types new envelope fields after gen-api.
- `models/mock.py` — child delegation is unit-testable end to end
  without a DB or real model (fake queue/stream in unit, mock children
  in integration).

## Confirmed decisions (ADR 0018 carries the contract)

### D51 — one `sub__`/`handoff__` dynamic family; route-level publish pinning; cycle 422

Descriptors minted by `SubAgentToolProvider.resolve(bindings, ctx)`
(full ExecutionContext captured; run-scoped values live on the tool
instances). Binding config `{agent_id, agent_version_id}` — the pin
SERVER-STAMPED at `PATCH /v1/agents/{id}` (the `_pin_agent_versions`
pattern), client pins overwritten, never trusted; 422 no-published-
version/foreign target; stale pins = detail lints (D1 replay fidelity,
one level up — the D42 argument transposed). Publish-time cycle walk
over pinned snapshots (Kahn's; direct self-binding 422); acyclic by
construction with server-stamped pins — the check is defense-in-depth.
Merged tooling: one registry + ToolRuntime + aclose for mcp__ + sub__ +
handoff__ together. `SubAgentResolutionError` under a new shared
`ToolResolutionError` base (McpResolutionError re-bases; three catch
sites catch the base). Name is display-only; identity from
`config.agent_id`.

### D52 — child runs are ordinary queue runs; the tool blocks, bounded

`queue_message` + `create_queued_run` per child (eval composition;
run_id pre-generated; shared trace_id; parent's absolute deadline;
session_id=None; metadata `{"sub_agent": {"depth": n}}`). The tool
awaits `PgEventStream.subscribe(child_run_id)` to the terminal:
completed → child `final_message` as output; failed → honest
`is_error` result; cancelled → re-raise. Non-terminal endings all
bounded: cancel observed → `request_cancel(child)` + re-raise;
stream-ended-without-terminal (pause frame, defense-in-depth) →
`request_cancel` + error result; parent deadline while waiting →
`request_cancel` + error result (REQUIRED: the sweeper never reaps
pending rows — without this bound the parent blocks forever).
Usage read off the child's TERMINAL EVENT (`RunCompleted.total_usage`),
never the row. Crash semantics recorded: sweeper fails the parent; the
child (independent claim) runs to completion, fully visible + linked.

### D53 — `parent_run_id`: the pre-declared additive set

`_Event.parent_run_id: str | None = None` (ADR 0003 amendment; D43
node_id precedent); `RunQueueMessage.parent_run_id: str | None = None`
(`extra="forbid"`, additive-optional); `ExecutionContext.parent_run_id`
+ `RunResult.parent_run_id` (domain); `ExecutionRepo.list_runs` gains
the `parent_run_id` filter keyword; `InProcessEventSink` stamps the
field in `_append` (the one choke point — no event factory exists),
constructed by `worker._execute_claimed` from the message; migration
0012 adds the nullable column + index on `agent_executions` (a real
column: cancel fan-out + list filter need a queryable index, not a
metadata scan). Threading closed: ExecutionContext is built in exactly
`worker._context` + `cli/main.py`; `finish_run` never writes the column.

### D54 — budgets compose at the parent; cancellation fans out

The tool reports the child usage in `ToolResult.metadata["usage"]`
(persisted in tool_executions — free auditability); the runtime folds
tool-reported usage into `ctx.usage` right after `execute` (`Usage
.plus`). `RunLimits.exceeded` at the next iteration top clamps the
whole tree (`token_budget`) — verified no terminal bypasses the
checkpoint. Parent cancel reaches the blocked tool via the shared
token between stream batches → cancel child + re-raise; cancelling
the child directly propagates honestly (`run.cancelled` → parent
re-raises). Iterations: delegation is ONE parent iteration; the
child's caps apply to the child unchanged (two-caps rule per level).

### D55 — children are non-interactive, bounded, honestly flagged

Queue-message metadata `auto_decline_approvals: true` → gated calls
take the EXISTING decline branch (ToolCallDeclined + refusal message,
copy parameterized — "no human approver available") without pausing;
`AskHumanStep` in a child auto-answers ("No human is available in
this run — proceed from what you know." appended, loop continues,
re-asks capped by iterations). Depth rides metadata
(`sub_agent.depth`); `JARVIS_SUB_AGENT_MAX_DEPTH` (default 2) —
over-cap spawn = recoverable error ToolResult, never a terminal.
Memory-on-child = publish-time lint on the parent (the
`memory_ignored` pattern, ADR 0015). `ToolRuntime` unbounded sentinel:
`timeout <= 0` (annotation or config) disables `wait_for`; existing
tools byte-identical (no tool uses `<= 0` today — regression-gated);
sub/handoff descriptors mint `timeout: -1`, `requires_approval: False`.

### D56 — handoffs: successor run carries the rendered transcript

The `handoff__` tool enqueues a successor (pinned target, shared
trace_id + session_id, parent deadline, parent_run_id, metadata
`{"handoff": {"note": ...}}` + depth) whose INPUT is the predecessor
transcript rendered as labeled text (`list_messages(parent_run_id)`,
truncated from the start to a Settings char cap) + the note. Explicit
transfer, not session-sharing — conversations are per-(agent_id,
session_id); cross-agent sessions share NOTHING (verified). Recorded
limitations: the successor's own memory starts fresh; same-agent
handoffs with memory enabled continue through the ordinary path. The
runtime ends the predecessor at the handoff: after the batch,
`LoopOutcome(kind="handoff", ...)` → `_after_loop` → ordinary
`run.completed` terminal, final_message naming target + successor run
(status "succeeded"; no new event type, no new status, envelope
closed). Linkage queryable: successor row's parent_run_id +
`metadata.handoff`; no event-metadata change.

## Commit sequence

1. **Planning** (this commit): ADR 0018 + this plan + D51–D56 in
   decisions.md + roadmap S14 planning record. No S14 code.
2. **Envelope + domain + ports + sentinel**: `_Event.parent_run_id`;
   `ExecutionContext`/`RunResult` fields; `RunQueueMessage.parent_run_id`
   (pre-declared); `ExecutionRepo.list_runs` filter keyword
   (pre-declared); `InProcessEventSink` parent stamping +
   `worker._context`/sink threading; the `ToolRuntime` `timeout <= 0`
   unbounded sentinel. Unit: envelope additivity (old payloads replay
   as None), sink stamping, message parse, sentinel byte-identical
   default + unbounded path.
3. **Persistence**: migration 0012 (`parent_run_id` + index) +
   `_run_row`/`_load_run` + scoped view + the `list_runs` filter.
   Integration: column round-trip, filter, tenant scoping.
4. **Provider + family**: `SubAgentToolProvider` (resolve(bindings,
   ctx), descriptor minting, captured run-scoped values),
   `SubAgentResolutionError` + `ToolResolutionError` re-base + three
   catch sites, merged mcp+sub tooling in `_resolve_tooling` (no
   sub/mcp bindings → byte-identical), depth cap + settings, child
   enqueue service (the eval composition extracted for reuse), the
   bounded blocking subscribe wait + all three non-terminal branches.
   Unit: provider resolution failures, cycle-adjacent 422s (route
   level), tool mapping (completed/failed/cancelled/cancel-observed/
   deadline/wait-ended), fake queue + fake stream.
5. **Runtime: usage fold + non-interactive children**: fold
   `ToolResult.metadata["usage"]` after execute; auto-decline branch
   (parameterized copy) + auto-answer branch. Unit: fold into ctx
   (token_budget fires at next checkpoint), decline without pause,
   ask answered without pause, copy wording.
6. **Publish pinning + cycles + lints**: `PATCH /v1/agents` pin
   overwrite, 422s, cycle walk, agent-detail `lints` (stale pins,
   memory-on-child). Unit + integration: pin overwrite beats client
   pins, self-binding 422, A→B→A 422, stale-pin lint.
7. **Handoffs**: `handoff__` tool (transcript rendering + successor
   enqueue + metadata), the post-batch handoff outcome branch +
   `_after_loop` mapping (RunCompleted naming the successor).
   Unit: transcript render + truncation, successor message shape,
   outcome mapping, mixed batch (other calls persist first).
8. **API + capabilities + SSE keep-alive**: executions
   `?parent_run_id=` route + detail `children`; capabilities flips
   (agents.detail.sub_agents, executions.detail.sub_agents); the SSE
   PING keep-alive (pre-existing gap, ADR §Consequences — emit on the
   1s wake timeout so a blocked parent's stream stays alive).
   Integration: supervisor → two mock sub-agents end to end (children
   queued, claimed, events, usage folded, parent succeeds); cancel
   parent mid-child → both cancelled; child failure → honest parent
   tool result; handoff chain (transcript in successor input, parent
   completes naming successor); tenant 404s; capabilities derived.
9. **Web**: gen-api regen; agent-editor sub-agent/handoff picker
   (agent select → `sub__`/`handoff__` binding, `{agent_id}` config;
   server stamps the pin); ExecutionDetail children section (labeled
   sub-agent children + handoff successor links) + parent link;
   capabilities accessors; msw handlers + fixtures. Gates: tsc,
   eslint, vitest.
10. **Joint manual session + closure** (post-build, Harshit): draft
   `docs/walkthrough-s14.md` during the build (uncommitted by design);
   the manual session runs it live (gemma supervisor delegating to
   workers; a handoff chain; cancel; the web tour); then the closure
   commit — walkthrough with session notes, README multi-agent
   section + API rows, roadmap S14 shipped note, CLAUDE.md gotchas
   from the build.

## Rule-7 audit (what touches what)

| Touch | File(s) | Frozen-surface? |
| --- | --- | --- |
| `_Event.parent_run_id` (additive-optional) | `domain/events.py` | **THE envelope change — pre-declared ADR 0018 §3** |
| `RunQueueMessage.parent_run_id` (additive-optional) | `ports/queue.py` | **THE ports/ change #1 — pre-declared ADR 0018 §3** |
| `ExecutionRepo.list_runs` gains `parent_run_id` keyword | `ports/repository.py` | **THE ports/ change #2 — pre-declared ADR 0018 §3** |
| `ExecutionContext`/`RunResult` fields | `domain/execution.py` | domain, additive; listed in ADR §3 |
| `ToolResolutionError` base | `tools/mcp/errors.py` | internal exception base — McpResolutionError re-bases; catch sites widen to the base |
| `ToolRuntime` `timeout <= 0` sentinel | `tools/runtime.py` | tools/ behavior change — regression-gated byte-identical |
| migration 0012 | `persistence/migrations/versions/` | new column + index |
| Sub-agent provider + family | `tools/subagents/` (new), `api/deps.py` | new module; registration mirrors builtins |
| `_resolve_tooling` composition, usage fold, handoff outcome, auto-decline/answer branches | `runtime/agent_runtime.py` | internal runtime; no sub/mcp bindings → byte-identical |
| Pinning + cycle 422 + lints | `api/routes/agents.py` | route layer (the workflows pattern) |
| Executions filter + children detail, capabilities, SSE PING | `api/routes/executions.py`, `capabilities.py`, `sse.py`/`_run_routes.py` | additive routes + the pre-existing PING gap |
| Web picker + detail links | `web/src/sections/agents/AgentEditor.tsx`, `sections/executions/ExecutionDetail.tsx` | capability-gated; gen-api regen |
| worker claim path | — | **zero changes** (children are `kind="agent"`) |
| event types / terminal semantics / queue contract | — | **frozen** |

## Acceptance (roadmap S14, adapted to the shipped scope)

- A supervisor agent delegates to two workers via `sub__` tools; the
  children are indistinguishable from manual runs in `/executions`
  (same rows, same event model), linked by `parent_run_id`.
- The parent's token budget bounds the whole tree (child usage folded;
  an over-budget tree fails `token_budget` at the parent checkpoint).
- A cancelled parent cancels its children at their checkpoints; the
  parent never blocks forever (unclaimed-child bound; deadline bound).
- `/v1/executions/{parent}` shows child runs; a child's detail links
  its parent; a handoff's successor carries the predecessor transcript
  rendered in its input and the predecessor completes naming it.
- Children never pause: approvals auto-decline, asks auto-answer (honest
  copies); depth beyond the cap is a recoverable tool error, never a
  terminal; cycles are 422 at publish.
- No `sub__`/`handoff__` bindings → agent runs byte-identical
  (regression-gated); a foreign tenant's sub-agent reads as absent.
- Web: the agent editor binds sub-agents/handoff targets; the
  executions detail links parent/child; sections flip from
  capabilities, never hardcoded.