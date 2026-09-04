---
name: frontend-code-review
description: Use only when the user explicitly requests a review or audit of frontend code under `web/`. Supports pending-change, file-focused, and pasted-diff reviews. Do not use for implementation-only requests, diagnosis without review intent, or backend-only code.
---

# Frontend Code Review

Review the requested scope for concrete, reproducible regressions. The root
`CLAUDE.md` owns project facts; `docs/architecture/frontend-architecture.md`
owns the frontend policy. This skill owns the review workflow and routes to
its bundled rule packs.

## Evidence First

1. Establish the review scope from the requested files or current diff.
2. Read the changed lines, their behavior owner, and the nearest section
   directory (`web/src/sections/<section>/`).
3. Trace the backing API endpoint and the capabilities payload only when
   they decide correctness.
4. Report only findings tied to an observable failure, a violated
   frontend-architecture contract, a security boundary, or a demonstrated
   maintenance risk.

## Rule Routing

Read only the packs matched by the diff:

- Navigation, section enablement, coming-soon panels, empty states, any
  new screen's data source: [`references/honest-ui.md`][honest]
- API client usage, error handling, SSE/stream consumption, optimistic
  updates: [`references/data-and-streaming.md`][data]
- Component ownership, state placement, hooks, types, Tailwind usage:
  [`references/component-quality.md`][quality]

## Severity And Output

- **P0**: security or privacy leak, data loss, production crash, or
  inaccessible critical workflow.
- **P1**: user-visible regression, invalid API contract usage, broken
  primary interaction, or **any fake functionality** (rendered capability
  the backend doesn't have — a direct violation of decision 1.6).
- **P2**: concrete maintainability, performance, test, or accessibility
  defect likely to cause incorrect behavior.
- **P3**: minor actionable cleanup; omit unless the user requested a
  thorough audit.

Lead with findings ordered by severity. Include a tight file and line
reference, the failing contract or reproduction path, impact, and a
concrete fix direction. If there are no findings, say `No issues found.`
and state any material verification gap. Do not add praise sections,
speculative risks, or an unsolicited offer to implement fixes.

[data]: references/data-and-streaming.md
[honest]: references/honest-ui.md
[quality]: references/component-quality.md