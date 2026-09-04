# Ownership & State

Authority: `docs/architecture/frontend-architecture.md` (app structure,
stack), Dify lessons in `docs/reference/dify-map.md` (draft-save,
`_`-state convention, registry mirroring).

## Placement

- `src/sections/<name>/` owns everything section-specific: routes' screens,
  section components, section stores. A section directory is the unit of
  enablement — when its capability flips on, its directory ships.
- `src/components/` is for contracts shared by two or more sections (event
  timeline, JSON viewer, form primitives). One-consumer components don't
  belong here; premature sharing freezes an API before two real users
  exist.
- `src/api/` owns the generated client, envelope unwrapping, and the SSE
  module. Components never import fetch/EventSource directly.
- `src/capabilities/` owns the payload hook and the `ComingSoon` panel —
  the only code allowed to branch on enablement.

## State

- **Server state (agents, executions, conversations, versions)**: TanStack
  Query. Query keys are centralized per resource so mutations invalidate
  precisely (`agents`, `agent(id)`, `agent-versions(id)`, `executions`,
  `execution(id)`).
- **Editor drafts**: a zustand slice per editor surface. The draft object
  may carry `_`-prefixed runtime keys (focus targets, collapsed panels);
  a save serializes through one function that strips `_`-prefixed keys
  and sends the server hash for optimistic concurrency. Never persist
  `_`-state.
- **Run console**: a zustand slice holding cursor, connection phase
  (connecting/live/replaying/ended), and the coalesced event timeline.
  Component subscribes; the SSE module writes. Resume = re-enter the
  slice with the stored cursor.
- **URL state**: list filters and selected tabs in the router so links
  are shareable; not in slices.

## Effects

Allowed: SSE connect/disconnect (a named external system), focus
management, subscription to a document-level event. Anything else —
derive during render or move to the owning action handler. An Effect that
syncs state between two components is a placement bug, not an Effect.