# Data & Streaming

Authority: `docs/architecture/frontend-architecture.md` (stack + key UX
surfaces), `docs/decisions.md` D13–D16,
`docs/architecture/event-model.md` (event union).

## The contract

- **Typed client generated from `/openapi.json`** — no hand-written
  request/response types duplicating backend schemas; drift should be
  impossible by construction.
- **Every error has one envelope**: `{"error": {kind, message, details}}`.
  The client unwraps it in one place; components surface `kind`/`message`,
  not raw fetch errors. No `catch` that swallows the envelope shape.
- **SSE in one module**: framing, `id: <cursor>` tracking,
  `Last-Event-ID` reconnect, terminal-close detection, and delta
  coalescing live in the single SSE client (`web/src/api/`). Components
  consume a typed event stream; they never parse `text/event-stream`
  themselves.
- Run-console semantics: reconnect resumes from the last seen cursor —
  events must be delivered exactly-once to the UI; finished runs replay
  through the same component path as live runs.
- The event union mirrors the backend's discriminated union — new event
  types must degrade gracefully (unknown type → render as generic timeline
  entry, never crash).
- TanStack Query owns server state (agents, executions, conversations);
  optimistic updates only where the backend has a real confirm path
  (e.g. draft-save with server hash). Mutations that publish versions
  invalidate the versions query.

## What to flag

- Hand-duplicated backend types in TS; envelope handling outside the
  client; per-component SSE parsing; reconnect without cursor tracking.
- Event renderers that assume a fixed set of `type`s (crash on new
  backend event types).
- Query keys that can go stale across sections (agent edited in editor
  but list not invalidated).
- Streaming state stored in component state where a zustand slice or
  Query cache is the agreed owner (see how-to-write-component).

## Severity calibration

- Lost/duplicated events on reconnect, or envelope shape broken: **P1**.
- Crash on unknown event type: **P1** (breaks the forward-compat contract).
- Stale-key bugs, type duplication: **P2**.