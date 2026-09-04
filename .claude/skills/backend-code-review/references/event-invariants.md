# Event Invariants

Authority: ADR 0003, `docs/decisions.md` D2/D3/D15/D16,
`docs/architecture/event-model.md`, `CLAUDE.md` rules 2 and 7.

## The contract

- One envelope: `{event_id, run_id, sequence, created_at, type}` — the
  shape is **frozen**. New event *types* require an ADR; field changes
  require an ADR.
- Per-run `sequence` is gapless and sink-assigned; the global
  `execution_events.cursor` BIGSERIAL *is* the SSE `Last-Event-ID`.
- **Exactly one terminal event per run** (`run.completed` / `run.failed` /
  `run.cancelled`), emitted by `sink.finalize()` — only the orchestrator
  may call it, exactly once. No non-terminal event after a terminal one.
- Blocking `/run` and streaming `/stream` execute the same
  `AgentRuntime.run()` and must produce identical event sequences
  (test-guarded — D14). The SSE route subscribes *before* the run task
  starts.
- Live-run resume prefers the live in-process sink; finished-run resume
  replays from the DB by cursor (D3). Same cursor space either way.
- Terminal events are committed atomically — no race between finishing and
  persisting may produce two terminal rows or none.

## What to flag

- Anything appending events outside the sink, assigning `sequence` in
  application code, or emitting events after `finalize()`.
- A new event type added without an ADR, or a field added to the envelope.
- SSE framing/delta handling outside the single SSE module (`api/sse.py`).
- A route that runs the runtime differently between blocking and streaming
  paths.
- Tests that assert event order without asserting terminal-uniqueness
  (weakest assertion wins).

## Severity calibration

- Missing/duplicate terminal emission, sequence gaps, cursor misuse:
  **P0/P1** — these are the platform's replayability guarantees.
- Envelope shape change without ADR: **P1** (process violation even if
  code is correct).