# S7 manual walkthrough — OTel tracing (ADR 0021), live script

A hands-on reproduction of the S7 acceptance behavior against a live
stack. Format follows walkthrough-s14. Backend :8001, web :5173, dev DB
`jarvis` (migration 0015), mock agents for the deterministic steps.

S7 in one sentence: **every run's work is recorded as finished spans in
Postgres, grouped by the run's own trace_id** — the run's trace_id IS a
valid OTel trace id (D73), spans cover five kinds at the real seams
(D74), and an optional OTLP exporter ships the same spans out without a
TracerProvider anywhere in the process (ADR 0021).

The invariants the walkthrough exercises:

- **D71/D74 — the recorder, not a TracerProvider.** Spans arrive at the
  sink already FINISHED: `run.segment` per segment, `iteration` per loop
  pass, `llm.call` per model invocation (the TracingModelClient wrap),
  `tool.call` per executed call, `knowledge.retrieve` per retrieval.
  Declined tool calls get no span (the S14 no-tool-row fact).
- **D72 — persistence always on, export additive.** The Postgres sink
  runs by default (`JARVIS_SPANS_ENABLED`, kill-switch); OTLP is OFF
  until `JARVIS_OTEL_EXPORT_ENABLED=1`, and a broken exporter endpoint
  must never fail a run (sink failures are logged and swallowed).
- **D73 — one trace covers the conversation.** Children (sub-agent
  delegations, handoff successors) and trigger-fired runs copy the
  parent's trace_id verbatim; a webhook inbound carrying a W3C
  `traceparent` joins the producer's trace.
- **D77 — the read side is honest.** `GET /v1/traces` groups by
  trace_id (newest first, run_ids + statuses + kinds); the detail route
  returns the spans chronological; the observability capability flips
  WITH the routes, never before.

## 0. Setup

The backend must be restarted to pick up the S7 routes + spans table
(migration 0015):

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0015: spans
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

auth_mode is `required` in `.env` — reuse your session identity:

```bash
export JARVIS_KEY="jarvis_sk_…"       # your existing api key
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity — the observability capability is live and names its detail:

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sections"]["observability"])'
# → {"enabled": true, "summary": …, "detail": {"spans": true, "otel_export": false, "service_name": "jarvis"}}
```

## 1. A mock run's spans — the record side, before any UI

Create a mock agent and run it (the mock provider needs
`strategy.type: function_calling` — its default reply is not
ReAct-parseable):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "s7-demo", "model": {"provider": "mock", "model": "mock-model"},
  "strategy": {"type": "function_calling"}
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
export S7_AGENT="<paste-id>"
RUN_ID=$(curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$S7_AGENT/run -d '{"input": "what is the plan today?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
echo "RUN_ID=$RUN_ID"
```

Receipt 1 — the run row carries a 32-hex trace_id, and it is NOT the
run_id:

```bash
export TRACE_ID=$(curl -s -H "$AUTH" localhost:8001/v1/executions | python3 -c "
import json,sys
run = next(r for r in json.load(sys.stdin)['items'] if r['run_id'] == '$RUN_ID')
print(run['trace_id'])")
echo "$TRACE_ID"                # 32 hex, no dashes
```

Receipt 2 — the trace list groups the run's spans under exactly that id:

```bash
curl -s -H "$AUTH" localhost:8001/v1/traces | python3 -m json.tool | head -20
# → traces: [{trace_id: $TRACE_ID, run_ids: [$RUN_ID], span_count: ≥3,
#    kinds: [iteration, llm.call, run.segment], statuses: [ok], …}]
```

Receipt 3 — the detail route returns the spans chronological, with the
segment → iteration → llm.call parent chain:

```bash
curl -s -H "$AUTH" localhost:8001/v1/traces/$TRACE_ID | python3 -c '
import json,sys
spans = json.load(sys.stdin)["spans"]
for s in spans:
    print(s["kind"].ljust(18), s["name"].ljust(12), s["status"].ljust(4),
          "parent=" + (s["parent_span_id"] or "root")[:8], f"{s["duration_ms"]}ms")'
# → run.segment  <agent>  ok parent=root       …ms
#   iteration         ok parent=aaaaaaaa…      …ms
#   llm.call          ok parent=bbbbbbbb…      …ms
```

A mock run's spans are honest about being cheap: durations in the low
milliseconds, the llm.call span naming the provider in its attributes.

## 2. The Observability UI

At `http://localhost:5173/observability`:

- the list shows the trace(s) — id prefix, run count, span count, kinds,
  status pill green;
- click into the trace — the waterfall: the segment row at depth 0, the
  iteration indented under it, the llm.call under the iteration, each
  with offset + duration + a duration bar sized against the trace's own
  span;
- an unknown trace id in the URL (e.g. `/observability/000…`) shows the
  backend's 404 and a "Back to traces" link — the disabled-observability
  503 would render the same shape, not a fake empty state.

## 3. An error run's spans — failure is recorded, not hidden

The deterministic live failure (mock never resolves credential_ref —
S13 lesson): an `openai_compatible` agent with a dead base_url. The
strategy wraps the model call, so the terminal kind is `strategy`, and
the spans say WHERE it died:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "s7-dead", "model": {"provider": "openai_compatible", "model": "x",
  "base_url": "http://127.0.0.1:9/v1",
  "credential_ref": {"type": "env", "env_var": "JARVIS_UNUSED_KEY"}},
  "strategy": {"type": "function_calling"}
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
export DEAD_AGENT="<paste-id>"
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$DEAD_AGENT/run -d '{"input": "go"}' | python3 -m json.tool | head -4
```

Wait for the worker to finish (the ConnectError is fast), then:

```bash
curl -s -H "$AUTH" localhost:8001/v1/executions | python3 -c "
import json,sys
run = next(r for r in json.load(sys.stdin)['items'] if r['agent_id'] == '$DEAD_AGENT')
print(run['status'], run['error_kind'])
export TRACE_ID='$TRACE_ID'" 2>/dev/null
export DEAD_TRACE=$(curl -s -H "$AUTH" localhost:8001/v1/executions | python3 -c "
import json,sys
print(next(r['trace_id'] for r in json.load(sys.stdin)['items'] if r['agent_id'] == '$DEAD_AGENT' and r['status'] == 'failed'))")
curl -s -H "$AUTH" localhost:8001/v1/traces/$DEAD_TRACE | python3 -c '
import json,sys
spans = json.load(sys.stdin)["spans"]
for s in spans:
    if s["status"] != "ok":
        print(s["kind"], s["status"], (s["status_message"] or "")[:90])'
# → llm.call error … / run.segment error … — the failure is a persisted
#   span state, never an exception past the runtime (D5).
```

In the UI the trace now shows red status rows; the LIST colors the trace
red because `statuses` includes "error".

## 4. One trace across agents — the sub-agent child

Reuse the S14 supervisor if it still exists (or build the pair per
walkthrough-s14 §1–2): a `sub__` binding makes the parent block on a
child run. Run the supervisor and let it delegate, then check that
BOTH runs share ONE trace:

```bash
curl -s -H "$AUTH" localhost:8001/v1/traces | python3 -c '
import json,sys
for t in json.load(sys.stdin)["traces"]:
    if len(t["run_ids"]) > 1:
        print("trace", t["trace_id"], "covers", len(t["run_ids"]), "runs")'
curl -s -H "$AUTH" localhost:8001/v1/traces/<MULTI_RUN_TRACE> | python3 -c '
import json,sys
spans = json.load(sys.stdin)["spans"]
runs = {s["run_id"] for s in spans}
print("run_ids in trace:", len(runs))'
# → 2 — the child's segment/iteration/llm.call spans land in the parent's
#   trace (D73: the child copies the parent's trace_id), and the detail
#   route renders the whole conversation as one waterfall.
```

## 5. Webhook inbound joins the producer's trace

A webhook trigger with a signed body whose headers carry a W3C
traceparent — the fired run copies the header's trace_id (D76). Build
per walkthrough-s13 §2 (HMAC-SHA256 over the raw body, hex digest) and
add the header:

```bash
python3 - << PY
import hashlib, hmac, json, urllib.request
secret = b"<webhook-secret>"          # from the trigger create response
payload = json.dumps({"input": "trace me"}).encode()
sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
TRACE = "11223344556677889900112233445566"   # 32 hex, nonzero
req = urllib.request.Request(
    "http://localhost:8001/v1/triggers/<TRIGGER_ID>/webhook", data=payload,
    headers={
        "X-Jarvis-Signature": sig,
        "traceparent": f"00-{TRACE}-abcdef0123456789-01",
    })
print(urllib.request.urlopen(req).status)
PY
# → 202
```

The fired run's row carries `trace_id == 11223344556677889900112233445566`
— the ingest joined the producer's trace instead of minting its own.
`GET /v1/traces/11223344…` now returns the fired run's spans.

## 6. OTLP export — off by default, and a broken endpoint fails nothing

The default is OFF (`otel_export: false` in capabilities). Enabling it
against a dead endpoint must NOT take runs down — the sink failure is
logged and swallowed (ADR 0021 §5):

```bash
kill %1 2>/dev/null   # stop the backend
JARVIS_OTEL_EXPORT_ENABLED=true \
JARVIS_OTEL_ENDPOINT=http://127.0.0.1:9/v1/traces \
JARVIS_OTEL_SERVICE_NAME=jarvis-demo \
  uv run jarvis serve > /tmp/jarvis-8001-otel.log 2>&1 &
sleep 3
curl -s -H "$AUTH" localhost:8001/v1/capabilities \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sections"]["observability"]["detail"])'
# → {"spans": true, "otel_export": true, "service_name": "jarvis"}
```

Run the mock agent again — the run still SUCCEEDS (the Postgres sink is
unaffected), the log carries the swallowed OTLP export failure, and the
spans in Postgres are identical to the export-off run:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$S7_AGENT/run -d '{"input": "again"}'
grep -i "otlp\|export" /tmp/jarvis-8001-otel.log | tail -3
```

Restart the backend without the OTLP vars to leave the stack default.

## Findings

(to be filled during the joint session — the S8 ritual)