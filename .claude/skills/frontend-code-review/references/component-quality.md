# Component Quality

Authority: `docs/architecture/frontend-architecture.md` (app structure),
the `how-to-write-component` skill (ownership/state decisions — apply it
when reviewing structure), general TypeScript/React practice.

## The contract

- **Section ownership**: code lives in `web/src/sections/<section>/`
  (agents, executions, …); `web/src/components/` holds only genuinely
  shared UI (event timeline, JSON viewer, forms). A shared component
  used by exactly one section is misplaced.
- **State placement per the ownership table**: server state in TanStack
  Query at the lowest consumer; editor/draft/canvas state in zustand
  slices; URL state in the router. Component-local state stays local.
- **Draft-save convention** (Dify lesson): `_`-prefixed keys are runtime
  state and are stripped before save; drafts save with a server hash for
  optimistic concurrency.
- TypeScript: no `any` in section/component code; discriminated unions
  for event/option types; exhaustive switch with a fallback, not
  truthiness checks on `type` strings.
- Tailwind: canonical utilities over arbitrary values; no one-off
  `style={{}}` where a token/utility exists; class soup extracted into
  the owning component.
- Accessibility on interactive surfaces: the run console (live region for
  streamed deltas), forms in the agent editor (labels, disabled states
  that reflect *capability* state, not just input validity), keyboard
  access for the event timeline.

## What to flag

- A component in `components/` with one consumer; state lifted higher
  than its lifetime requires; prop drilling past a zustand/Query owner.
- `any`, non-exhaustive `switch` on event/union types, `as` casts that
  defeat the generated client's types.
- `style={{}}`/hardcoded hex where a design token exists.
- Disabled buttons that don't explain why (capability vs. validation);
  missing aria-live on streamed content.

## Severity calibration

- Type casts hiding a broken contract: **P2** (P1 if it can crash a
  render path). Structure/ownership issues: **P3** unless they force
  duplication that will drift. Accessibility on the run console: **P2**.