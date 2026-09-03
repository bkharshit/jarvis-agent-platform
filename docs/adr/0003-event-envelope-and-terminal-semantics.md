# ADR 0003 — Event envelope + exactly-one-terminal semantics

- **Status**: Accepted
- **Date**: 2026-09-04

## Context

Every execution must be reconstructible from persisted events, and SSE
consumers must be able to resume. Dify's `dify-agent` protocol
(`protocol/schemas.py`, `runtime/event_sink.py`) uses a type-discriminated
event union with a terminal event that *is* the status transition, and a
cursor-based replay store.

## Decision

1. One shared envelope: `{event_id, run_id, sequence, created_at, type}` —
   `sequence` is **per-run, gapless, sink-assigned**.
2. `ExecutionEvent` is a discriminated union (see
   `docs/architecture/event-model.md`).
3. **Exactly one terminal event per run** (`run.completed` / `run.failed` /
   `run.cancelled`). `EventSink.finalize()` accepts terminal events only,
   is once-only, and raises on any append after a terminal event.
4. The global DB cursor (`execution_events.cursor` BIGSERIAL PK) doubles as
   the SSE `Last-Event-ID`.

## Convariants (unit-tested)

- Gapless per-run `sequence` (0, 1, 2, …).
- Exactly one terminal event; no non-terminal event may follow it.
- Terminal event fields carry the run summary (final message, total usage,
  iterations / error kind / cancel reason).

## Consequences

- Any consumer can reconstruct a run by replaying events in sequence order.
- The orchestrator (ADR 0004) is the *only* component allowed to finalize.
- Queue-backed sinks (future) keep the same contract; only the transport
  changes.