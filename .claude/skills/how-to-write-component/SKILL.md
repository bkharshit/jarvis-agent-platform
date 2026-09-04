---
name: how-to-write-component
description: Use when implementing or refactoring React/TypeScript components under `web/` and the task requires decisions about component ownership, section boundaries, state, data flow, effects, or interaction ownership. Do not use for review-only requests, test-only work, copy-only edits, or styling-only changes.
---

# How To Write A Component

Use this skill to route component architecture decisions to its bundled
reference. Read only the references required by the current change.
`docs/architecture/frontend-architecture.md` is the policy owner for IA and
stack; this skill owns the placement decisions.

## First Decisions

| Question | Default | Choose differently when |
| --- | --- | --- |
| Where should code live? | In the owning section (`src/sections/<section>/`). | Two or more sections need the same stable contract (→ `src/components/`). |
| Who owns server state? | TanStack Query at the lowest consuming component. | A mutation elsewhere must invalidate it (share the query key module). |
| Who owns editor/draft state? | A zustand slice scoped to the editor surface. | The value must survive unmount (persisted draft) or cross sections (then question the placement first). |
| Should a value be component state? | Yes, for ephemeral UI state only. | The value drives rendering across siblings — lift to the slice, not to a parent prop-drill. |
| Who owns run-console streaming state? | The SSE store slice (cursor, connection phase, coalesced events). | Never component state — reconnect/resume spans component lifetimes. |
| Is a wrapper needed? | Use the shared component or direct code. | The wrapper owns behavior, validation, state, or semantics. |
| Is an Effect needed? | Derive during render or handle the user action. | A named external system (SSE connection, document title) must be synchronized. |
| Disabled or hidden? | Disabled + reason, when the backend may enable it later (capability). | Hidden, only when the payload says the section doesn't exist at all. |

## Topic Routing

- Component moves, section boundaries, props, placement: read
  [`references/ownership-and-state.md`][ownership].
- Query keys, mutations, version publishing, SSE consumption: read the
  frontend-code-review pack
  [`../frontend-code-review/references/data-and-streaming.md`][data].

## Workflow

1. Identify the behavior owner, the required state lifetime, and the
   public contract being changed.
2. Read the nearby implementation, the section's existing components, and
   only the routed reference.
3. Implement one coherent vertical slice; do not expand into equivalent
   patterns elsewhere unless the current contract cannot be completed
   without them.
4. Verify observable behavior at the narrowest sufficient boundary, then
   run the frontend gates from `CLAUDE.md` (`tsc --noEmit`, eslint,
   `vitest`).

[data]: ../frontend-code-review/references/data-and-streaming.md
[ownership]: references/ownership-and-state.md