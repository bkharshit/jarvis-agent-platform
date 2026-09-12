# ADR 0014 — LLM trace visibility (in-memory buffer + executions route)

- **Status**: Accepted (built 2026-09-12)
- **Amends**: the JARVIS_LLM_TRACE print-and-forget contract of commit
  585aae7 — the log stays, and a web-readable in-memory buffer joins it.
  Nothing in `ports/`, the event envelope, or the error shape changes;
  the trace never becomes a persisted event (that would store full
  prompts in `execution_events` and change the envelope — rejected).

## Context

`JARVIS_LLM_TRACE=true` logs every model call's actual request (messages
including the system prompt, which is never persisted) and response to the
backend log. Harshit's debugging loop lives in the web UI — the log is a
second stop. The trace has to live somewhere an API route can read.

## Decision

### 1. In-memory buffer — still nothing persisted

`LlmTraceBuffer` (runtime-internal, `runtime/llm_trace.py`) is a bounded
registry keyed by `run_id`: an `OrderedDict` keeping the most recent ~20
runs, evicting the oldest. Entries are structured dicts (iteration, method,
provider/model, timestamp, request payload, response payload or null).
Nothing touches Postgres, the event stream, or any event type — a restart
loses it, by design. **Rejected**: persisting trace rows (new table +
migration, or a new event type in `execution_events`) — stores full prompts
durably, bloats the event log, and reverses the debug-only spirit.

### 2. Route: `GET /v1/executions/{run_id}/llm-trace`

Reads the container's buffer after the standard scoped executions guard:
foreign run → 404 `not_found` (D29), nested read authorized by the parent
lookup (the `persistence/scoped.py` convention). Empty `entries` is a 200,
never an error — the UI explains the empty state (flag off, restart, or a
distributed worker holds the buffer).

### 3. Disclosure via capabilities, not a new section

`sections.executions.detail.llm_trace` (boolean = the settings flag) —
`detail` blocks are the established per-feature flag surface
(`human_in_the_loop` precedent). The disabled `observability` section (S7,
"traces and spans") stays reserved for the real observability stage.

### 4. Embedded worker is the supported shape

With `JARVIS_EMBEDDED_WORKER=true` (default) the worker and API share one
process, so the route sees everything. Distributed mode (`false` + separate
`jarvis worker` processes) keeps the buffer in worker processes the API
cannot read — the trace view degrades to empty; **the backend log is the
distributed-mode trace**. Documented, not engineered around: this is a
debug feature, and a DB-backed trace was explicitly rejected.

### 5. Security posture

Payloads carry the system prompt, transcript, and tool schemas — never
secret material: credentials ride HTTP headers, never the `ModelRequest`
(ADR 0005/0006). The flag defaults off; when off, no buffer fills and the
capabilities flag reads false, so the UI section never renders. Tenancy is
enforced by the route guard, not the buffer.

## Consequences

- Debugging prompt drift / bad tool calls happens entirely in the UI:
  run → execution detail → expandable per-iteration request/response.
- The capabilities fetch is once-per-session (`staleTime: Infinity`), so
  flipping the flag requires a backend restart **and** a page reload.
- The buffer is process-local: horizontal scaling or worker separation is
  invisible to the trace view (log only). Revisit only if a persisted
  trace becomes a product need (that belongs to S7 observability).