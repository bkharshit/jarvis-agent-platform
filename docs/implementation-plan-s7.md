# Implementation plan — S7: OTel tracing (ADR 0021)

Format follows implementation-plan-s8.md. Every commit: gates green
(`uv run pytest tests/unit`, `uv run ruff check src tests`,
`uv run ruff format --check src tests`, `uv run mypy src`) before
committing; integration (`-m db`) where marked; web gates
(tsc --noEmit, eslint, vitest) on the web commit. One commit per step.
No S7 code exists at time of writing; all seams verified 2026-09-24.

## Verified seams (read in code, 2026-09-24)

See ADR 0021 "Context" for the full annotated list. The short version:

- **Trace identity:** minted once per run at enqueue
  (`api/routes/_run_routes.py:70`, `runtime/trigger_firing.py:112`);
  copied verbatim by the worker (`runtime/worker.py:373,527`), child
  runs and handoff successors (`runtime/child_runs.py:39-59`), and the
  queue message (`ports/queue.py:70`). `uuid4().hex` = 32 hex = an OTel
  trace_id verbatim.
- **Event cursors:** `InProcessEventSink.append` returns the durable
  cursor (`events/bus.py:74-117`) — span attributes can name exact
  replay positions.
- **Client wrapping:** `TracedModelClient` (`runtime/llm_trace.py`)
  composes in `_run_segment` (`agent_runtime.py:310-311`) — the
  `TracingModelClient` composes at the same seam, and BOTH wrap when
  both are on (order irrelevant: pure delegation).
- **Instrumentation sites in `_loop`** (`agent_runtime.py:435-757`):
  iteration top (`:466-505` — IterationStarted cursor), tool execute
  (`:664-686` — ToolCallStarted cursor, execute, ToolCallCompleted),
  declines (`:628-663` — NO span, matching the no-tool-row fact).
- **`ToolContext` is a plain mutable class** (`domain/tools.py:38-73`)
  — additive `trace_id` attribute is a non-event.
- **Webhook ingest:** `api/routes/triggers.py:277-317` — the one
  unauthenticated route (D60); `fire()` (`trigger_firing.py:84`)
  mints `trace_id` at `:112` and gains the optional kwarg.
- **Migration head is `0014_knowledge`**; tenant-table house style per
  `0014`; loose run ids, no FK (the D57 provenance pattern).
- **Capabilities:** `"observability"` flag exists disabled
  (`api/routes/capabilities.py:52`); flip + derived detail land in the
  SAME commit as first route usage (S4 lesson). Web placeholder route
  exists (`routes.tsx`, `SectionPlaceholder`).
- **Golden gates:** `tests/unit/test_agent_runtime_golden.py` pins
  event sequences — the runtime is byte-identical when `span_sink is
  None`.

## Confirmed decisions (ADR 0021 carries the contract)

- **D71** — `SpanRecord` (domain, pydantic: span_id/trace_id/
  parent_span_id/run_id/name/kind/started_at/ended_at/duration_ms/
  status/status_message/attributes) + `SpanSink` Protocol
  (`export(spans)`); runtime imports NO opentelemetry anything.
- **D72** — Postgres persistence ALWAYS on (`SqlSpanRepo` implements
  the port; `JARVIS_SPANS_ENABLED` kill-switch, default true); OTLP
  export additive behind `JARVIS_OTEL_*` (real SDK: TracerProvider +
  BatchSpanProcessor + OTLPSpanExporter HTTP, service.name Resource);
  container fans records out to both sinks.
- **D73** — OTel trace_id == run trace_id (`uuid4().hex` verbatim);
  children/successors share the trace by the existing copy; span_id
  minted per span; cross-process parentage rides `parent_run_id`
  attributes, not OTel parent links (v1).
- **D74** — five span kinds at existing seams: `run.segment`,
  `iteration`, `llm.call` (TracingModelClient), `tool.call`,
  `knowledge.retrieve`; declined calls get no span; event-cursor
  attributes; buffered recorder flushed at iteration/pause/terminal in
  a finally, sink failures logged-and-swallowed (D5 transposed).
- **D75** — settings: `spans_enabled`, `otel_export_enabled` (false),
  `otel_endpoint`, `otel_service_name`, `otel_headers_env` (D18
  pattern); deps `opentelemetry-sdk` +
  `opentelemetry-exporter-otlp-proto-http`, lazy import in the
  adapter.
- **D76** — W3C traceparent: webhook ingest parses it →
  `fire(trace_id=...)` joins the caller's trace; `ToolContext.trace_id`
  (additive) → `HttpGetTool` sends `traceparent` outbound.
- **D77** — `GET /v1/traces` + `GET /v1/traces/{trace_id}`
  (tenant-scoped); capabilities `observability` flips WITH the routes;
  web `/observability` + `/observability/:traceId` (trace list + span
  waterfall).

## Commit sequence

1. **`feat(s7): spans table, domain SpanRecord, SpanSink port,
   SqlSpanRepo`** — migration `0015_spans` (loose run_id, tenant_id,
   trace/span ids, kind, timings, status, JSONB attributes; indexes
   trace_id / run_id / tenant+started_at); `domain/spans.py`
   (`SpanRecord`, kind Literal, status Literal); `ports/spans.py`
   (`SpanSink` Protocol); `SqlSpanRepo.export` (batch INSERT, tenancy
   from the record). Unit repo tests + integration `-m db` (migration +
   roundtrip).
2. **`feat(s7): runtime span instrumentation — recorder, five span
   kinds, TracingModelClient`** — `runtime/spans.py` (buffered
   `SpanRecorder`, failure-isolated flush); instrumentation in
   `agent_runtime.py` (segment/iteration/tool/retrieval) +
   `TracingModelClient` composed beside `TracedModelClient`; the
   `span_sink: SpanSink | None = None` constructor seam; golden gates
   green unchanged (byte-identical when None). Unit tests: span shapes,
   cursors, error status, flush-on-failure, no-sink identity.
3. **`feat(s7): OTLP exporter adapter + settings + container wiring`**
   — `config.py` five settings; `observability/otel.py`
   (`OtelSpanSink`, lazy SDK import, explicit SpanContext); pyproject
   deps; `deps.py` wiring (fanout recorder sinks: SqlSpanRepo +
   optional OtelSpanSink). Unit tests: settings decode, adapter
   converts records (fake exporter), headers-env resolution, disabled
   paths.
4. **`feat(s7): W3C traceparent — webhook inbound, http_get outbound`**
   — `fire(trace_id=...)` optional kwarg (validation: nonzero 32-hex);
   webhook route parses `traceparent`; `ToolContext.trace_id` additive;
   `HttpGetTool` emits `traceparent`; unit tests both directions.
5. **`feat(s7): traces routes + capabilities flip`** —
   `api/routes/traces.py` (list summaries grouped by trace_id, detail
   spans ordered by started_at), schemas, `app.py` router,
   capabilities `observability` flip + `{otel_export, service_name}`
   detail. Integration `-m db`: runs produce queryable traces, tenancy
   isolation, empty state.
6. **`test(s7): traces e2e — spans land end-to-end`** — integration:
   a mock run produces run.segment + iteration + llm.call + tool.call
   spans in one trace_id; retrieval span when a knowledge binding
   exists; child run shares the parent trace; failed run carries error
   status; webhook traceparent roundtrip.
7. **`feat(s7): web observability — trace list + span waterfall`** —
   gen-api regen, `api/queries/traces.ts`, `sections/observability/`
   (TraceList + TraceDetail waterfall), routes replace the
   placeholder, msw fixtures + TEST_CAPABILITIES flip; vitest coverage
   (render, empty state, waterfall rows, error coloring, 404).
8. **Docs closure** (after the ONE manual session, per the working
   agreement) — `docs/walkthrough-s7.md`, README observability section
   + API rows, roadmap S7 shipped note, CLAUDE.md gotchas if the build
   earned any.

## Rule-7 audit (what touches what)

- `ports/` — ONE new Protocol (`SpanSink`); no existing port changes.
- `domain/` — ONE new module (`spans.py`); `ToolContext` gains one
  additive plain-class attribute (D76); no event envelope, no error
  shape, no execution-domain change (no ADR-required contract change —
  the new port IS the ADR).
- `runtime/` — `agent_runtime.py` instrumentation guarded by a None
  sink (byte-identical golden gates); `trigger_firing.py` optional
  kwarg; `worker.py`, `child_runs.py` UNTOUCHED (trace_id already
  flows).
- `api/` — one new router; capabilities detail flip in the same
  commit.
- `web/` — the `observability` SectionGate placeholder swaps for real
  screens; no other section touched.

## Acceptance (roadmap S7, adapted to this scope)

- A run's spans (segment/iteration/llm/tool/retrieve) are queryable in
  Postgres grouped by the run's persisted `trace_id` — no new plumbing
  anywhere in the run path.
- With `JARVIS_OTEL_EXPORT_ENABLED=true` + a collector endpoint, the
  same spans arrive via OTLP with the service name set and the run's
  trace_id as the OTel trace id (children included).
- A webhook producer sending `traceparent` makes the fired run join
  the caller's trace id; `http_get` forwards `traceparent` to the
  target.
- The web Observability section lists traces and renders the span
  waterfall from the persisted table; sections below the flag stay
  honest (`SectionGate`).
- All gates green per commit; golden event-sequence tests untouched
  (byte-identical runtime when the sink is None).