# Honest UI (capability gating)

Authority: decision 1.6 in `docs/decisions.md`,
`docs/architecture/frontend-architecture.md` (development model + IA table),
`CLAUDE.md` rule 6. This is JARVIS's most distinctive frontend contract.

## The contract

- **The backend decides what is enabled.** Nav items, routes, and section
  entry points render from the `GET /v1/capabilities` payload. The
  frontend never hardcodes a section as enabled/disabled, never infers
  enablement from a feature flag file, and never "try/catches" an API call
  to discover availability.
- **No fake functionality.** Every pixel renders real API state:
  - No mocked/seeded/demo data in place of API responses, including
    "nice-looking" placeholder content.
  - No inert buttons, no controls that silently do nothing, no sections
    that look alive but error on first use.
  - Empty states are *real* empty states ("no agents yet — create one"),
    not fabricated content.
- **Disabled sections use the shared `ComingSoon` component**: section
  name, one-line summary, the roadmap stage that enables it. No bespoke
  per-section stubs, no marketing pages for unbuilt features.
- **Registries mirror the backend**: tool lists, strategy names, model
  info come from API responses, never hardcoded arrays in the frontend
  (Dify lesson — frontend registry mirrors backend registry).
- Anti-scope (do not review into existence, but flag if built): screens
  for unbuilt sections beyond `ComingSoon`; auth UI before S2 (the shell
  runs anonymous/local mode with a notice); React Flow before S6.

## What to flag

- A hardcoded `enabled: true`/`false`, a capabilities type that drifts
  from the backend payload, or a section reachable by URL that the
  payload says is disabled (render the coming-soon panel instead).
- Mock data, demo fixtures, seeded arrays rendered as if real.
- A coming-soon panel that doesn't name its enabling stage.
- Hardcoded tool/strategy/model lists that will drift from the backend.

## Severity calibration

- Rendered capability the backend doesn't have (faked feature):
  **P1** — the core product-trust violation. Mocked data masquerading as
  real: **P1**. Missing stage label on a coming-soon panel: **P2**.