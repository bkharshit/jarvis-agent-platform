# ADR 0011 — Per-call tool approval decisions

- **Status**: Accepted
- **Date**: 2026-09-07
- **Amends**: ADR 0010 (§3 trigger classes, §4 resume contract), ADR 0008 (queue message), ADR 0003 (event union — §4 below)

## Context

ADR 0010 froze the resume body as exactly one of `{content}` (strategy
pauses) or `{tool_approval: bool}` (approval pauses). That boolean is one
decision for the *whole* gated batch: a pause carrying three pending calls
can only be approved or rejected in full. The web pause card rendered an
Approve/Reject pair per call, but every button posted the same
`{tool_approval}` — a UI that promised a per-call decision the contract
does not support.

The runtime underneath is already per-call: a resumed batch enters the
loop with a `refusals: set[str]` of declined call ids; each refused call
is closed with a refusal tool message ("user declined execution", no
started/completed events — it never ran) while the rest of the batch
executes. Only the contract is batch-shaped.

A human overseeing tool use genuinely needs the mixed case — approve the
calculator, decline the outbound `http_get` — and S4 (MCP tools, external
side effects) will make it unavoidable.

## Decision

### 1. The resume body gains a third variant: `decisions`

`POST /v1/executions/{run_id}/resume` accepts **exactly one** of:

```jsonc
{"content": "..."}                            // strategy pauses — unchanged
{"tool_approval": true | false}               // batch shorthand — unchanged
{"decisions": {"<call_id>": true, ...}}       // per-call — NEW (ADR 0011)
```

- `decisions` maps the pause's pending `tool_call_id`s to the human's
  verdict: `true` approves that call, `false` declines it.
- **Default-deny: a pending call absent from the map is declined.** An
  unmentioned call never runs — silence is rejection, never approval. The
  map must be non-empty; unknown call ids are ignored (the run's pause
  event is the authority on what is pending).
- `tool_approval` stays: it is the CLI/walkthrough shorthand and the
  one-call fast path. `true` ≡ all-approve, `false` ≡ all-decline.

### 2. Contract surfaces (the rule-7 change list)

- `ports/queue.py` — `ResumeRequest.kind` becomes
  `Literal["content", "tool_approval", "decisions"]` with
  `decisions: dict[str, bool] | None`; a model validator enforces
  kind/field coherence (the payload rides the queue unchanged — the JSONB
  merge in `enqueue_resume` is shape-agnostic).
- `api/schemas.py` — `ResumeBody` gains `decisions`; the validator
  requires exactly one of the three, non-blank content, non-empty map.
- `runtime/agent_runtime.py` — `_resume_segment` accepts `kind ==
  "decisions"` on a `tool_approval` pause exactly like the batch path,
  but computes `refusals` as the declined subset:
  `{c.id for c in pending if not decisions.get(c.id, False)}`. Mixed
  outcomes need **no loop change** — the loop already executes the batch
  minus `refusals`, closing each refused call with its refusal message.
- No event, envelope, or status change. The pause event's
  `pending_calls` is unchanged; the *decisions* live only in the resume
  append and the resulting transcript (refusal messages / tool
  executions).

### 3. A refusal becomes an event: `tool.call.declined`

ADR 0010 §3.1 gave a declined call no events at all ("it never ran" — no
`started`/`completed`). Correct about execution, but it left the *human
decision* invisible to the log: the refusal lived only in the transcript's
tool message, and a replayed timeline showed the call stuck at
`requested` forever — indistinguishable from a run cancelled mid-flight.

The refusal branch now emits one event per declined call, alongside the
refusal tool message:

```python
class ToolCallDeclined(_Event):
    type: Literal["tool.call.declined"] = "tool.call.declined"
    tool_call_id: str
    name: str
```

- New event type in the `ExecutionEvent` union (the ADR 0003 amendment
  this ADR already carries). Not terminal; sits mid-segment in the
  resumed segment like any other tool event — terminal-adjacency is
  untouched.
- The rule "a declined call never *executed*" still holds: no
  `started`/`completed`/`failed`, no `tool_executions` row. The declined
  event records the *decision*, not an execution.
- Replay ≡ live: the store folds it into a distinct `declined` card
  status, so the pause → decision → outcome chain reads the same in a
  live stream and a replayed log.

### 4. What stays frozen

- The event envelope, terminal semantics, segment invariants, queue
  mechanics, and the exactly-one-answer blocking-resume behavior (ADR 0010
  §4–§6) are untouched. `decisions` is one more `ResumeRequest` shape
  through the same `enqueue_resume` merge.
- Ungated calls in a resumed batch still execute regardless of decisions —
  they were never gated (unchanged from `tool_approval` semantics).
- A `decisions` answer against a strategy pause degrades exactly like the
  existing mismatched kinds: the loop re-invokes the strategy with the
  human's input absent, per ADR 0010 §4's stale-kind rule.

## Consequences

- The web pause card becomes a staged selection: per-call Approve/Reject
  toggles, an "allow all" shortcut, and one Submit that posts the map —
  the UI can no longer imply that clicking one row's button decided one
  call.
- API consumers that wrong a call id by typo get default-deny for it (the
  call is declined, the run continues) rather than an error — the run is
  never stuck on a malformed decision; the transcript shows what happened.
- S4 (MCP tools) inherits per-call gating without another contract change:
  external tool calls arrive as ordinary pending calls in `pending_calls`.