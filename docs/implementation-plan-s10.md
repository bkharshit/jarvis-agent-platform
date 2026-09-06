# JARVIS — Implementation Plan (Stage S10: human-in-the-loop)

- **Status**: In progress
- **Date**: 2026-09-06
- **Rides on**: ADR 0010 (this stage's contract record: `run.awaiting_input`,
  `awaiting_input` status, resume segments), ADR 0003 (envelope — amended by
  0010, exactly-one-terminal unchanged), ADR 0004 (orchestrator owns limits —
  pause is a runtime decision; limits bound the whole run), ADR 0008 (queue
  message; resume is one more segment), D5 (a run never raises — pause is a
  persisted non-terminal state, resume never escapes the runtime's try),
  D29 (404 not 403 for foreign runs), roadmap §S10.

## Context

Runs are fire-and-forget: no mid-run question, no tool approval, no
clarifying prompt. S10 adds resumable pauses. Because S1 already landed,
pauses are durable across processes from day one — the roadmap's
"in-process Phase-2a" compromise is not needed; a resume lands on any
worker.

The stage was chosen over S3 (plugin strategies) at the user's decision
(2026-09-06): lifecycle change before more stages stack on the runtime.

Verified seams this stage rides on (read in code, not from docs):

- `domain/events.py` — discriminated `ExecutionEvent` union; `is_terminal`
  over `TERMINAL_EVENT_TYPES`; `validate_event_sequence` asserts
  completed runs.
- `domain/execution.py` — `ExecutionStatus` Literal + `TERMINAL_STATUSES`;
  `RunResult` maps 1:1 to the run row.
- `runtime/agent_runtime.py` — the loop: `check_limits` checkpoints,
  tool-batch execution, `LoopOutcome`; `run()` upserts the RUNNING row and
  finalizes exactly once.
- `runtime/worker.py` — claim → execute → ack; heartbeat renews the lease
  and pops cancels; the sweeper's expired-lease policy (requeue iff 0
  events, terminal failure otherwise) covers crashed resume segments with
  no new code.
- `events/bus.py` — `InProcessEventSink` assigns the per-run gapless
  sequence as `len(self._events)`; `persistence/repositories.py
  append_event` trusts `event.sequence` (`event.sequence or 0`) — the
  resume segment's sink MUST be sequence-offset-seeded or it collides.
- `persistence/repositories.py` — `SqlRunQueue` rows keyed by unique
  `run_id` with `payload` JSONB (the only place the `principal` survives
  after enqueue — resume must merge into it, not replace it);
  `next_event_sequence` / `count_events` already exist (sweeper + worker
  protocols).
- `ports/tools.py` + `domain/tools.py` — `ToolDescriptor.annotations` and
  `ToolBinding.config` are already free-form; `requires_approval` needs no
  schema change, only a runtime check.
- `ports/strategy.py` — `StepOutcome` discriminated by `kind`; S10 adds
  the `ask_human` variant (ports change, recorded in ADR 0010).
- `api/routes/executions.py` + `agents.py` — `_await_terminal_row`,
  stream hold-back of the terminal frame, `PgEventStream.subscribe` stop
  conditions: all extend from "terminal" to "segment end".
- `web/src/` — run console `applyEvent` reducer, ExecutionStatus badges,
  capabilities-gated sections; the executions detail gains
  `human_in_the_loop` to gate the inbox.

## Confirmed decisions (ADR 0010)

1. **`run.awaiting_input` is a pause, not a terminal.** The envelope union
   gains one type; `TERMINAL_EVENT_TYPES`/`is_terminal` unchanged; a
   paused run has no terminal event yet. Segment invariant: no
   non-terminal event after it within a segment.
2. **A run is a chain of segments over one event log** — gapless sequence
   continues across pauses; the resume segment's sink is seeded with
   `next_event_sequence` (offset parameter on the sink implementation,
   not the port).
3. **Pause mechanics:** the loop returns without `finalize()`; the run row
   is marked `awaiting_input` + `awaiting_until`; the queue row is
   **acked** (no heartbeat holds a paused run — cancel/reap act directly
   on the row).
4. **Two trigger classes:** tool approval (descriptor `annotations
   .requires_approval` or binding `config.requires_approval`, binding
   wins; checked before the batch executes; reject = refusal tool
   messages for gated calls only) and strategy asks (`AskHumanStep` in
   `ports/strategy.py`).
5. **Resume = queue upsert-merge** (`RunQueue.enqueue_resume(run_id,
   resume)`): the payload's principal/deadline/identity are preserved,
   `resume` merged in, row flipped to `pending`. `RunQueueMessage` gains
   `resume: ResumeRequest | None`. A stale resume claim acks and skips.
6. **Limits bound the whole run:** resumed ctx seeds usage from the run
   row and iterations from persisted `iteration.started` events; the
   original deadline still applies.
7. **No run is ever stuck:** `Settings.awaiting_input_timeout_seconds`
   (default 86400) → sweeper pause-reap path (`awaiting_until < now` →
   `run.cancelled`, reason `awaiting_input timeout`); cancel on a paused
   run finishes it directly (immediate, not cooperative).
8. **Streams end at segment end** — both subscribe paths stop at
   `run.awaiting_input` like a terminal; the pause frame is held back
   until the row shows `awaiting_input`; clients re-attach with
   `Last-Event-ID` after resume.

## Design

### Data model (migration 0006)

- `ALTER TYPE execution_status ADD VALUE 'awaiting_input'` (autocommit
  block, the 0002 pattern; downgrade moves rows to `failed` first — the
  enum value itself cannot be dropped).
- `agent_executions.awaiting_until timestamptz NULL` (queryable copy of
  the pause deadline; NULL on non-paused runs, cleared on resume/finish).
- New repo methods: `mark_awaiting_input(run_id, awaiting_until)`,
  `expired_awaiting(now) -> list[run_id]`; `finish_run` also clears
  `awaiting_until`.

### Domain + ports

- `RunAwaitingInput` event (ADR 0010 §1) + union membership; new
  `PAUSE_EVENT_TYPE` / `is_pause()` helper; segment-invariant test in
  `test_domain_events.py`.
- `ExecutionStatus` gains `awaiting_input`; `RunResult.status` carries
  it; `TERMINAL_STATUSES` unchanged.
- `ports/strategy.py`: `AskHumanStep(StepOutcome)` — `kind="ask_human"`,
  `question: str`.
- `ports/queue.py`: `ResumeRequest {kind: "content"|"tool_approval",
  content, approved}` + `RunQueueMessage.resume` + `RunQueue
  .enqueue_resume(run_id, resume)`.

### Runtime

- `PauseOutcome` (reason, question, pending_calls) joins `LoopOutcome`;
  `run()` returns `status="awaiting_input"` and marks the row instead of
  `finish_run` when the loop pauses.
- Tool-approval check in the batch branch; `AskHumanStep` branch beside
  `FinishStep`.
- `resume(version, run_id, ctx, sink, resume_request)`: messages rebuild =
  system prompt (PromptEngine) + persisted `list_messages(run_id)` +
  resume append (user message for `content`; approval outcome → execute
  batch or refusal tool messages for `tool_approval`); seed usage/iteration
  from the row and event log; conversation append for memory agents; then
  the shared loop. Model resolution inside the try (D28).
- Approval-required detection helper: `binding.config` first, then
  `descriptor.annotations`.

### Worker / queue

- `SqlRunQueue.enqueue_resume` — JSONB `||` merge, reset to pending.
- Claim branch: `message.resume is not None` → resume segment (sink with
  sequence offset; stale-resume guard: row must be `awaiting_input`, else
  ack + skip).
- On pause result: `mark_awaiting_input` + ack.
- Sweeper: `reap_awaiting(now)` — `expired_awaiting` rows get
  `run.cancelled` (reason `awaiting_input timeout`) at the next sequence +
  `finish_run` + queue cleanup.
- `WorkerExecutions` protocol gains what the resume path uses
  (`list_messages`, `mark_awaiting_input`, `expired_awaiting`,
  `count_iteration_starts`… — structural additions only).

### API

- `POST /v1/executions/{run_id}/resume` — `{content}` or
  `{tool_approval: bool}`; 404 unknown/foreign, 409 not awaiting; blocks
  until segment end (like `/run`), returns the run row.
- Cancel route: `awaiting_input` branch finishes directly.
- `run`/`stream`/`events` SSE: stop at pause; `_await_terminal_row`
  extends to segment end; hold-back applies to the pause frame.
- Schemas: `ResumeRequest`, `RunResult` status union, capabilities
  `executions.detail.human_in_the_loop: true`.
- `Settings.awaiting_input_timeout_seconds = 86_400`.

### Web (UI enablement — the stage's last item)

- Regenerated API client (`make gen-api`).
- Run console: `run.awaiting_input` in `applyEvent` → approval card(s)
  (Approve/Reject per gated call) or question + input box; resume POSTs
  the route and re-attaches the SSE stream (`Last-Event-ID`).
- Executions section: `awaiting_input` status badge + awaiting-input
  inbox (filter `status=awaiting_input`), gated on
  `capabilities.executions.detail.human_in_the_loop`.
- Vitest + RTL per `frontend-testing`; no mocked data (decision 1.6).

## Commit sequence (each independently green)

| # | Commit | Gates |
|---|--------|-------|
| 0 | `docs(adr): ADR 0010 + S10 implementation plan + D31/D32` | docs only |
| 1 | `feat(persistence): awaiting_input status + awaiting_until (migration 0006) + repo methods` | unit + migration integration |
| 2 | `feat(domain,ports): RunAwaitingInput event, ask_human variant, ResumeRequest + enqueue_resume port` | unit |
| 3 | `feat(events): segment-end semantics — sink sequence offset + pause stop in subscribe paths` | unit |
| 4 | `feat(runtime): pause paths — tool approval + AskHumanStep; run() returns awaiting_input` | unit |
| 5 | `feat(runtime,worker): resume segments — runtime.resume + queue upsert-merge + stale-guard` | unit + integration |
| 6 | `feat(worker): pause-reap sweeper path + cancel-during-awaiting` | unit + integration |
| 7 | `feat(api): POST /executions/{id}/resume + SSE segment ends + capabilities flag` | unit + **full integration suite** |
| 8 | `feat(web): run-console pause UI + awaiting-input inbox` | tsc, eslint, vitest |
| 9 | `docs(s10): walkthrough, README, decisions` | docs only |

Every commit: `pytest tests/unit`, `ruff check src tests`, `mypy src`
green before committing; full integration suite (`-m db`) green on commits
1, 5, 6, 7; web gates green on commit 8.

## Risks & deliberate deferrals

- **Sequence collision on resume** — the offset-seeded sink is the guard;
  an integration test asserts the resumed segment's first event continues
  the gapless sequence (ADR 0010 §1).
- **Double-resume races** — the API 409s non-awaiting rows; the worker's
  stale-guard acks and skips; a raced resume content is lost by design
  (client reads the row). Documented in ADR 0010 §4.
- **Blocking `/run` now returns a non-terminal row** — clients (CLI, web)
  treat segment end as "needs a decision"; no Phase 1 behavior changes
  for agents that never pause.
- **Mock-provider ReAct gotcha (D19)** still applies: pause tests use
  `function_calling` fixtures or fixture strategies.
- **Deferred**: approval policies beyond per-binding booleans (quorum,
  expiry-then-auto-approve), resume via SSE-only (the blocking resume
  mirrors `/run`), pause webhooks/notifications (S13 triggers are the
  natural home), multi-approval batching UI beyond per-call cards.

## Verification (S10 exit criteria)

1. `pytest tests/unit`, `ruff check src tests`, `mypy src` green; full
   integration suite green (Phase 1 + S1 + S2 behavior unchanged for
   agents that never pause).
2. Acceptance (roadmap §S10): an agent pauses on an approval-gated tool,
   resumes through the API, and completes; a strategy-asked pause answers
   via `{content}` and completes; `Last-Event-ID` resume works across the
   pause; a paused-forever run is reaped by the deadline; terminal-
   adjacency unit tests pass (no non-terminal event after
   `run.awaiting_input` until resume).
3. Limits compose: iteration counter and token budget span segments; the
   original deadline still fires (timed_out), never a stuck run.
4. Isolation: resume 404s on foreign-tenant runs (D29); 409 on
   non-awaiting; cancel on a paused run is immediate.
5. Web: `tsc --noEmit`, eslint, vitest green; the run console renders
   real pause state only; the inbox lists only API-returned runs.
6. Manual walkthrough (`docs/walkthrough-s10.md`): pause/approve/reject/
   ask/resume/timeout/cancel scenarios against the local backend, with a
   copy-paste-safe appendix (the S2 lesson — show status, never
   fire-and-forget).