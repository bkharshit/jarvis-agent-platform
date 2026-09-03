# Frontend Architecture (Phase 2 — stub)

Anticipated seam: React/Vite/React Flow builder whose node registry mirrors
backend node types. Consumes the same `/v1` API; SSE rendering uses the same
event union. Draft-save with server hash for optimistic concurrency;
`_`-prefixed runtime-state convention stripped at save. Not started.