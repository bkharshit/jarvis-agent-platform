# ADR 0021 — OTel tracing (S7): a span port, Postgres-persisted spans, and an additive OTLP exporter

Status: accepted (planning ADR — no S7 code exists at time of writing)
Date: 2026-09-24
Supersedes: none (implements the roadmap S7 sketch; scope confirmed by
Harshit 2026-09-24 — the REAL OpenTelemetry SDK with an OTLP exporter
behind `JARVIS_OTEL_*` settings, off by default; spans persisted in
Postgres behind a `SpanSink` port so the Observability UI works with
zero exporter config; the UI reads the spans table, never the exporter)

## Context

`trace_id` is already generated, plumbed, and persisted on every
execution row — S7 is pure addition. Verified seams (paths checked
2026-09-24):

- **Minting:** every run's trace_id is minted exactly once at enqueue —
  `api/routes/_run_routes.py:70` (`trace_id=str(uuid4())`) for API runs,
  `runtime/trigger_firing.py:112` for trigger-fired runs. Everyone
  downstream copies it verbatim: the worker stamps the row from the
  message (`runtime/worker.py:373,527`), child runs and handoff
  successors copy the parent's (`runtime/child_runs.py:39-59` — the
  S14 shared-trace decision), and the queue message carries it
  (`ports/queue.py:70`).
- **The identity is already OTel-shaped:** `uuid4().hex` is 32 hex
  characters = exactly 128 bits — a valid OTel `trace_id` with zero
  conversion. A run and all of its children are one OTel trace by
  construction.
- **Event cursors are reachable:** `InProcessEventSink.append`
  returns the durable cursor (`events/bus.py:74-117`) — spans can be
  tied to exact replay positions in the event log.
- **The client-wrapper precedent:** `TracedModelClient`
  (`runtime/llm_trace.py:70-161`) wraps `ModelClient` by pure
  delegation — client-level wrapping catches EVERY strategy including
  plugins, and streaming stays byte-identical. The S14 lesson
  generalizes: instrumentation that wraps the client beats
  instrumentation that edits strategies.
- **The run never raises (D5):** instrumentation sits inside the
  runtime's try but must ALSO be failure-isolated the other way — a
  broken span sink or exporter must never change a run's outcome.
- **ToolContext is a plain mutable class** (`domain/tools.py:38-73`),
  not pydantic — an additive attribute is a non-event.

## Decision

### 1. The runtime sees only a `SpanSink` port — the real SDK lives at the adapter edge

The runtime builds `SpanRecord`s (a domain pydantic type) around its
known boundaries and flushes them to a `SpanSink` Protocol
(`ports/spans.py`). The runtime imports NO opentelemetry anything.

The alternative — instrumenting the runtime through `opentelemetry-api`
directly — was rejected deliberately: it would put a third-party API on
the orchestrator's critical path, couple unit tests to the tracer's
global provider state, and rely on ProxyTracer no-op semantics we do
not control. Our port keeps `runtime/` hermetic (the dependency rule's
"adapters are swappable by construction"), and the real SDK still does
all the wire work at the edge:

- `observability/otel.py` converts `SpanRecord` → a real SDK span
  (explicit `SpanContext`: trace_id = the run's trace_id, span_id =
  the record's), under a `TracerProvider` carrying
  `Resource(service.name)` and a `BatchSpanProcessor(OTLPSpanExporter)`
  (OTLP HTTP/protobuf). The SDK owns batching, retries, and OTLP
  encoding.
- `SqlSpanRepo` implements the SAME port and is ALWAYS installed —
  spans land in Postgres whether or not any exporter is configured.
  The container fans a run's records out to both.

The confirmed "real OTel SDK" choice is honored exactly where it
pays: the wire format, batching, and propagation helpers are SDK-owned;
the runtime seam stays ours.

### 2. Trace identity: the OTel trace_id IS the run's trace_id

`uuid4().hex` needs no translation. All spans of a run share it; child
runs and handoff successors already copy the parent's trace_id, so a
whole multi-agent conversation is ONE OTel trace with zero new
plumbing. `span_id` is minted per span (16 hex). Cross-process span
PARENTAGE is not wired (v1): a child run's spans carry `parent_run_id`
as an attribute (the D57 provenance pattern) and the UI groups by
`trace_id` — the run linkage the executions UI already shows.

### 3. Span taxonomy — five kinds, at seams that already exist

| Span | Where | Key attributes |
| --- | --- | --- |
| `run.segment` | wraps each segment (fresh or resumed) | entry, agent_id, agent_version_id, depth, trigger name, terminal status/error_kind, paused |
| `iteration` | per loop iteration in `_loop` | iteration number, `jarvis.event.cursor` (the IterationStarted cursor) |
| `llm.call` | `TracingModelClient` wrapper (the `TracedModelClient` pattern) | provider, model, method, input/output tokens, finish_reason, requested tool names; ERROR status on `ModelError` |
| `tool.call` | around `tool_runtime.execute` | tool name, call id, latency_ms, is_error, kind; `jarvis.event.cursor` (ToolCallStarted cursor) |
| `knowledge.retrieve` | around `_retrieve_knowledge` | dataset ids, top_k, hit count, query length |

Declined tool calls get NO span (they never executed — matching the
"an auto-declined approval leaves no tool-execution row" fact). Every
span carries `run_id`, `trace_id`, and `parent_span_id` where one
exists. Event-cursor attributes tie each span to the exact replay
position in the event log — the span view can link into the
transcript.

Deferred (not v1): per-workflow-node spans, HTTP server spans, and
client/server span semantics on outbound HTTP — the taxonomy covers
the agent loop, which is what the roadmap sketch names.

### 4. Buffering, flush, and failure isolation

A per-run recorder buffers finished `SpanRecord`s and flushes to the
sinks at iteration boundaries, at a pause, and at the terminal — in a
`finally` on every failure path, so a failed run still exports what it
did. The flush itself is wrapped: sink exceptions are logged and
swallowed (D5 transposed — observability never touches a run's
outcome). Spans are written ON END only (no open-span rows); a worker
crash loses its buffered spans — acceptable, spans are observability,
never correctness.

### 5. Settings — persistence on by default, export off by default

```
JARVIS_SPANS_ENABLED        (default true)  — the Postgres sink kill-switch
JARVIS_OTEL_EXPORT_ENABLED  (default false) — the OTLP exporter gate
JARVIS_OTEL_ENDPOINT        (default http://localhost:4318/v1/traces)
JARVIS_OTEL_SERVICE_NAME    (default jarvis)
JARVIS_OTEL_HEADERS_ENV     (default "") — NAMES an env var whose value
                               is "k=v,k=v" OTLP headers (the D18
                               secrets pattern: referenced, never stored)
```

`opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` join
the regular dependencies; the adapter imports lazily so nothing loads
unless export is enabled.

### 6. W3C traceparent — one hop in, one hop out

- **Inbound:** the webhook ingest route parses a `traceparent` header
  (any version, valid nonzero 32-hex trace id) and passes the trace id
  to `TriggerFireService.fire()` (new optional `trace_id` kwarg) — the
  fired run joins the CALLER's distributed trace instead of minting a
  fresh one; absent/invalid → fresh `uuid4()` as today.
- **Outbound:** `ToolContext` gains an additive `trace_id: str = ""`;
  `HttpGetTool` sends `traceparent: 00-<trace_id>-<fresh 16-hex>-01`
  when present, so a downstream service can continue the trace.
  v1 propagates context; it does not emit client spans.

### 7. Persistence, API, capabilities

Migration `0015_spans` — `spans` table: loose `run_id` (no FK — the
D57 pattern), `tenant_id`, `trace_id`, `span_id`, `parent_span_id`,
`name`, `kind`, `started_at`/`ended_at`, `duration_ms`, `status`,
`status_message`, `attributes` JSONB; indexes on `(trace_id)`,
`(run_id)`, `(tenant_id, started_at)`.

- `GET /v1/traces` — tenant-scoped summaries grouped by trace_id
  (root run, span count, first/last span, statuses).
- `GET /v1/traces/{trace_id}` — the spans, ordered by started_at.
- The `observability` capability flag flips WITH these routes in the
  SAME commit (the S4 capabilities-500 lesson), detail reporting
  `{otel_export: <bool>, service_name}` from Settings — derived facts,
  never hardcoded.

### 8. Web — the Observability section activates

`/observability` (trace list) and `/observability/:traceId` (span
waterfall, simple nested bars with duration/error coloring, clicking
through to the run) replace the placeholder, gated by the flipped
flag; msw fixtures + gen-api regen ride the same commit (the S11/S13
pattern).

## Consequences

- The Observability UI works on ANY deployment with zero exporter
  config (spans are in Postgres); OTLP is a pure opt-in for vendors.
- The runtime gains one constructor seam (`span_sink: SpanSink | None
  = None`) and is byte-identical when it is None — the golden gates
  pin this.
- A run's spans can be missing if a worker dies mid-run (write-on-end
  buffering) — documented, accepted; no correctness depends on them.
- New kinds of spans (workflow nodes, HTTP) extend the taxonomy
  without touching the port — `kind` is data, not a schema break.