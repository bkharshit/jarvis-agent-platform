# ADR 0010 — Human-in-the-loop: `run.awaiting_input`, resumable pauses, and the resume segment

- **Status**: Accepted
- **Date**: 2026-09-06
- **Amends**: ADR 0003 (event envelope union), ADR 0004 (orchestrator owns limits), ADR 0008 (queue message)

## Context

Phase 1 froze the event envelope (ADR 0003): a run emits exactly one
terminal event and nothing after it. Runs are fire-and-forget — the caller
cannot answer a mid-run question or approve a dangerous tool call. The
roadmap (S10) calls for "terminal-adjacent" pauses: a run stops *without
ending*, a human answers, and the run continues.

Everything a pause needs already exists as persisted truth: the event log
is reconstructible, messages are saved per run, and S1 made runs durable
across processes — so a pause can outlive the worker that entered it, and
a resume can land on any worker.

## Decision

### 1. New event type `run.awaiting_input` — pause, not terminal

```python
class RunAwaitingInput(_Event):
    type: Literal["run.awaiting_input"] = "run.awaiting_input"
    reason: Literal["tool_approval", "strategy"]
    question: str = ""                       # strategy-asked prompt text
    pending_calls: list[ToolCall] = []        # approval-gated calls, tool pauses
    awaiting_until: datetime                 # pause deadline (§6)
```

- `TERMINAL_EVENT_TYPES` and `is_terminal()` are **unchanged** — a paused
  run simply has no terminal event *yet*. Exactly-one-terminal still holds
  for the run's whole life; the pause sits in the middle of one gapless
  per-run sequence.
- **Terminal-adjacency invariant** (unit-tested, extending ADR 0003): no
  non-terminal event may follow `run.awaiting_input` in a *segment*. The
  segment ends there; the next event after it belongs to a resumed
  segment. Control flow enforces it (the loop returns after emitting it),
  not the sink.
- The sink is **not finalized** on pause. A second `append()`/`finalize()`
  on the same sink instance after a pause raises `EventSequenceError` — a
  segment has ended, just as after finalize. The next segment uses a **new
  sink seeded with a sequence offset** (`next_event_sequence`), because
  `append_event` trusts the sink-assigned sequence and a fresh sink would
  restart at 0 and collide (§5).
- The `ExecutionEvent` union (ADR 0003) gains this one type. No envelope
  field changes.

### 2. New `ExecutionStatus` value `awaiting_input` — not terminal

`ExecutionStatus` gains `"awaiting_input"`; `TERMINAL_STATUSES` does NOT
(terminal semantics extend, they don't change — a paused run can still
reach exactly one of `succeeded | failed | cancelled | timed_out`).
Migration adds the enum value (ALTER TYPE in an autocommit block, the 0002
pattern — Postgres cannot drop enum values, so downgrade migrates rows
out first) and one nullable column:

- `agent_executions.awaiting_until timestamptz NULL` — the pause deadline
  the sweeper scans (§6). The event carries the same instant for replay
  fidelity; the column is the queryable copy.

### 3. Trigger classes

1. **Tool approval.** `ToolDescriptor.annotations` (already the sanctioned
   extension point) or a binding's `ToolBinding.config` may set
   `requires_approval: true` — binding config wins over the descriptor, so
   any bound tool can be gated per-agent without a new tool. The
   orchestrator checks *before executing the batch*: it emits
   `tool.call.requested` for every call in the step, then pauses with the
   approval-gated subset in `pending_calls`. No `tool.call.started` until
   approved. **Approve** → the whole batch executes. **Reject** → every
   gated call gets a refusal tool message ("user declined execution");
   ungated calls in the same batch still execute. Approval decisions are
   the orchestrator's, not the tool's — the tool never learns it was gated.
2. **Strategy-requested input.** `ports/strategy.py` gains an
   `AskHumanStep` variant (`kind="ask_human"`, `question: str`), sibling of
   `FinishStep`/`ToolCallsStep`. The orchestrator persists the assistant
   message, emits `run.awaiting_input` with `reason="strategy"`, and
   returns. Built-in strategies don't ask; fixture strategies exercise it.

Both are runtime pauses, not model-layer concepts: the pause check is in
the loop the orchestrator owns (ADR 0004).

### 4. Resume: one more queue segment, same run

`RunQueueMessage` (ADR 0008) gains `resume: ResumeRequest | None`, where
`ResumeRequest = {kind: "content" | "tool_approval", content: str | None,
approved: bool | None}` — a ports/ contract change, recorded here. The
queue row for a paused run is `done` (the worker acked on pause), so resume
is an **upsert-merge**, a new `RunQueue.enqueue_resume(run_id, resume)`
port method: JSONB-merge `resume` into the existing payload (the original
`principal`, `deadline`, and identity fields are preserved — the run row
does not carry the principal, only the payload does) and flip the row back
to `pending`.

- `POST /v1/executions/{run_id}/resume` — body `{content}` (strategy
  pauses) or `{tool_approval: true | false}` (approval pauses). 404 for
  unknown/foreign runs (D29), **409** when the run is not
  `awaiting_input`. It blocks like `/run`: subscribe to the segment's
  events until they end (terminal **or** the next pause), then return the
  run row — interactive loops are chains of pause/resume calls.
- The worker's claim path branches on `message.resume`: a resume segment
  re-enters the **same `run_id` and pinned `agent_version_id`** (the
  version is the run row's, not "latest"). Messages rebuild as
  `system prompt + persisted run messages + resume append` — the runtime
  never replays the model, it resumes the conversation. A stale resume
  claim (row no longer `awaiting_input` — timeout reaped or raced) acks
  and skips; the client learns the truth by reading the run.
- **Limits bound the whole run, not the segment** (ADR 0004): the resumed
  `ExecutionContext` seeds `usage` from the run row and the iteration
  counter from the persisted `iteration.started` events, so
  `max_iterations` and token budget clamp the chain, and the original
  `deadline` (preserved in the payload) still applies at
  `check_limits()`.
- **No requeue for crashed resume segments.** A resumed run always has
  prior events, so S1's expired-lease policy lands on the terminal-failure
  branch ("worker lost"), never a blind re-execution — which the gapless
  sequence could not survive. Consistent with ADR 0008 §3.

### 5. Streams end at a pause (segment end = stream end)

- Both subscribe paths (`PgEventStream.subscribe`, the in-process sink)
  stop at `run.awaiting_input` exactly as they do at a terminal event: the
  client's stream ends, it fetches the run detail (now
  `awaiting_input`), renders the pause UI, and re-attaches with
  `Last-Event-ID` after resume — one cursor space, unchanged semantics.
- The pause frame is held back until the row is written (the existing
  hold-back pattern for terminal frames), so a stream that ends on a pause
  is followed by a detail fetch that already shows `awaiting_input`.

### 6. No run is ever stuck: pause deadline, reaper, and cancel

- `Settings.awaiting_input_timeout_seconds` (default 24h) sets
  `awaiting_until = now + timeout` at pause time, carried on the event and
  the row.
- The sweeper gains a pause-reap path (mirroring the expired-lease path):
  rows `awaiting_input` with `awaiting_until < now` get a
  `run.cancelled` (reason `awaiting_input timeout`) appended at the next
  sequence and the row finished — exactly one terminal, from any worker
  process.
- `POST /executions/{id}/cancel` on a paused run cannot use the queue
  heartbeat (no worker holds it): the route finishes it directly — the
  same append-terminal-and-finish path as the reaper. Cancelling an
  `awaiting_input` run is therefore immediate, not cooperative.

### 7. What stays frozen

- The envelope fields, `is_terminal`, `TERMINAL_EVENT_TYPES`, cursor
  semantics, cooperative cancellation, and the error envelope are
  untouched. Exactly-one-terminal holds over the whole run.
- `EventSink`'s port shape is unchanged (the sequence offset is a
  constructor parameter of the implementation, not a protocol change).

## Consequences

- A paused run is fully reconstructible: replay shows the pause, the resume
  is a user/tool message in the transcript, and the terminal event closes
  the chain — execution detail, conversations, and evaluations (S11) see
  interactive runs as ordinary runs.
- The blocking `/run` endpoint can now return a run in `awaiting_input`
  (a non-terminal row); clients must treat segment end, not terminal, as
  the "needs a decision" signal.
- `validate_event_sequence` still requires a terminal event — it validates
  *completed* runs; paused logs are validated by the segment invariant.
- S6 workflows and S14 multi-agent inherit pause/resume through the same
  envelope addition rather than a second mechanism.
- UI: the run console renders `run.awaiting_input` (approval cards /
  input prompt) with a resume action; the Executions section gains an
  awaiting-input inbox; capabilities report `human_in_the_loop` under the
  executions detail.