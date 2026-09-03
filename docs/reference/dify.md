    # Dify — exploration summary and verdicts

## What Dify is, structurally

Dify grew from a chat-app builder into a platform: workflow engine
(`api/core/workflow/`), model runtime indirection, a plugin daemon
(`api/core/plugin/`), an extracted agent service (`dify-agent/`), and a large
ReactFlow frontend (`web/`). The Sept 2026 checkout is mid-migration:
the agent loop is delegated to pydantic-ai inside a separate service, the
workflow engine and model runtime live in an external `graphon` pip package,
and a Go runtime handles sandboxed tool execution.

## Adopt

Protocol seams; engine layer/middleware; template-method tool invoke;
discriminated-union events with terminal semantics; event-sink port with
cursors; definition-snapshot-per-run; separate generate/stream; delta
coalescing; SSE + Last-Event-ID.

## Simplify

Single in-process monolith — no plugin daemon, no external agent service, no
Redis streams (Postgres + in-process bus suffice for Phase 1). Explicit
registries instead of import side effects. Typed JSONB instead of
LongText-JSON columns.

## Diverge

We own the agent loop in our runtime (Dify's new backend delegates to
pydantic-ai — we keep a hand-written loop behind a strategy protocol so
ReAct/FC/custom stay first-class). No god-class facades. No dual config
systems.

## Avoid (verified anti-patterns)

- `ToolManager` god class (~1200 lines) — the tool platform buried in a
  facade.
- Triple-overloaded stream unions (`invoke_llm(stream=...)` returning
  different shapes).
- 1100-line multi-protocol `node_runtime.py`.
- Runtime state leaking into persisted graphs.
- Boundaries leaking under pressure (poking `_execution_context`).

## The lesson

Dify is paying a large refactoring cost because early interfaces weren't
clean. JARVIS Phase 1 exists to set those seams *before* the surface area
grows.
