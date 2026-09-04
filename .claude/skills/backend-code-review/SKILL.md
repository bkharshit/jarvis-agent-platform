---
name: backend-code-review
description: Use only when the user explicitly requests a review or audit of backend code under `src/jarvis/`, `tests/`, or `persistence/migrations/`. Supports pending-change, file-focused, and pasted-diff reviews. Do not use for implementation-only requests, diagnosis without review intent, frontend code under `web/`, or docs-only changes.
---

# Backend Code Review

Review the requested scope for concrete, reproducible defects. The root
`CLAUDE.md` owns project facts and commands; this skill owns the review
workflow and routes to its bundled rule packs.

## Evidence First

1. Establish the requested review scope and inspect the relevant diff or files.
2. Read the changed lines, their behavior owner, nearby tests, and the local
   contracts (ADR, decision, or docstring) that define intended behavior.
3. Trace callers, persistence boundaries, event-sink usage, or model-adapter
   I/O only when they decide correctness.
4. Report only findings tied to an observable failure, a violated contract
   (ADR / `docs/decisions.md`), a security boundary, a data-integrity risk,
   or a demonstrated maintenance problem.

## Rule Routing

Read only the packs matched by the diff:

- Anything in `domain/` or `ports/`, or a new import crossing layers:
  [`references/dependency-rule.md`][dependency]
- New/changed events, sink or stream usage, SSE routes, cursor semantics:
  [`references/event-invariants.md`][events]
- Runtime loop, limits, cancellation, strategies, tool runtime behavior:
  [`references/runtime-semantics.md`][runtime]
- Tables, migrations, repositories, JSONB payloads, versioning:
  [`references/persistence.md`][persistence]
- Routes, request/response schemas, error handling, the capabilities
  payload: [`references/api-contracts.md`][api]

When no pack applies, review correctness, security, behavior changes, and
test evidence directly. Check official docs only when local code and
contracts do not settle framework behavior.

## Severity And Output

- **P0**: security or privacy exposure, data loss, or a production-wide outage.
- **P1**: user-visible regression, broken run-termination or tenant-isolation
  contract, invalid public API contract, or a failed primary workflow.
- **P2**: concrete correctness, performance, maintainability, or test defect
  likely to cause incorrect behavior.
- **P3**: minor actionable cleanup; omit unless the user requested a
  thorough audit.

Lead with findings ordered by severity. Include a tight file and line
reference, the failing contract or reproduction path, impact, and a concrete
fix direction. If there are no findings, say `No issues found.` and state any
material verification gap. Do not add praise sections, speculative risks, or
an unsolicited offer to implement fixes.

[api]: references/api-contracts.md
[dependency]: references/dependency-rule.md
[events]: references/event-invariants.md
[persistence]: references/persistence.md
[runtime]: references/runtime-semantics.md