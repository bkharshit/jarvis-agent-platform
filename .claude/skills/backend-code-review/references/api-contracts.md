# API Contracts

Authority: `docs/decisions.md` D13–D17, D19,
`docs/architecture/api.md`, `docs/implementation-plan.md` §5,
`CLAUDE.md` rules 2 and 7.

## The contract

- **One error envelope everywhere**: `{"error": {kind, message, details}}`
  — ApiError, FastAPI validation 422s, and unexpected 500s all map
  through it. No bare `HTTPException` text bodies.
- Routes are thin: parse → delegate to runtime/repos → serialize. Request
  and response schemas mirror domain models; the OpenAPI document is the
  frontend's client source of truth (generated client —
  `docs/architecture/frontend-architecture.md`), so response shapes are
  public contracts: don't rename fields casually.
- Domain models serialize directly — remember `ToolResult.output` is
  **top-level** in API JSON, not nested under `result` (D17).
- SSE is framing-only in one module; `/executions/{id}/events` returns
  JSON by default and SSE on `Accept: text/event-stream` — same cursor
  space (D16).
- Cancel is idempotent; delete returns 409 when executions exist; list
  endpoints paginate with `limit`/`offset`.
- `GET /v1/capabilities` is the **single authority for feature
  enablement** (decision 1.6): sections flip on there, never in frontend
  code. A capability payload that claims an unimplemented backend is a
  defect (a lie); a shipped backend feature absent from the payload is
  also a defect (invisible work).
- Agent YAML/JSON bodies pin their own `model.provider`; env settings are
  fallbacks only (D19).

## What to flag

- An error path returning anything but the envelope; a new route raising
  `HTTPException` instead of mapping through the error taxonomy.
- Route logic that should live in the runtime; duplicated serialization
  of domain models.
- Response-shape changes without checking the generated frontend client
  impact; missing schema on a route (breaking OpenAPI generation).
- Capabilities payload out of sync with what the backend actually
  implements.
- Missing tests for: envelope on each error class, idempotent cancel,
  SSE-vs-JSON cursor parity.

## Severity calibration

- Broken error envelope or a capabilities lie: **P1**. Casual field rename
  in a consumed response: **P1**; missing pagination/pagination bug: **P2**.