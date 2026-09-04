---
name: frontend-testing
description: Use when writing or changing vitest or React Testing Library tests under `web/`, or when the user explicitly requests frontend test strategy. Do not use for frontend code-review-only requests, backend tests (pytest), or generic testability discussion.
---

# Frontend Testing

`docs/architecture/frontend-architecture.md` (stack + F1 acceptance) is the
policy owner; `CLAUDE.md` owns commands. This skill adds no separate
requirements.

1. Identify the observable contract and the regression risk. A test that
   restates the implementation is not a contract.
2. Choose the smallest boundary that includes the behavior owner — the
   component (or hook) whose failure the test should catch.
3. Establish the failing case first when practical, then implement one
   coherent scenario.
4. Mock at the API client boundary (generated client / SSE module), never
   inside a component's imports — the client boundary is the honest seam.
5. Capability-gated behavior gets tested in both states: the section
   renders live content when the payload enables it, and the `ComingSoon`
   panel (naming its stage) when it doesn't.
6. Run the focused spec before the affected suite and `tsc --noEmit` +
   eslint (`CLAUDE.md` gates).
7. Report the behavior verified and any remaining browser, visual, or
   end-to-end risk.

Recommend deleting low-value tests as readily as adding missing behavior
coverage. E2E (Playwright, against the mock provider backend) comes after
F1 stabilizes — unit/integration coverage first; do not add a browser
suite speculatively.