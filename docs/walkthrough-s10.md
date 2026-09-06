# S10 manual walkthrough — human-in-the-loop pause/resume (ADR 0010), verified live

A hands-on reproduction of the S10 acceptance tests against a live stack,
with the "why" and the concept behind each step. Verified 2026-09-06
against dev DB `jarvis` (migration 0006 applied), backend on :8001, real
model `gemma4:31b` via Ollama Cloud (BYO key in `.env`, referenced by
*name* — never pasted into config, D29/D30).

One rule to keep in mind while reading (D31): **a pause is a segment, not
a terminal.** A paused run returns HTTP 200 with `status:
"awaiting_input"` and `finished_at: null` — a *successful* call whose run
is simply not finished. The gapless per-run event sequence continues
across the pause; terminal-adjacency says no non-terminal event may
follow `run.awaiting_input` until a resume or a reap appends the segment.

Two worker-side facts the walkthrough exercises (D32):

- **Resume is one more queue segment.** `POST /executions/{id}/resume`
  upsert-merges the answer into the run's queue payload and flips the row
  back to pending — any worker can claim it; the resumed segment rebuilds
  messages from the persisted transcript and keeps the original deadline.
- **Limits span the chain.** `max_iterations` and the token budget bound
  the whole pause/resume chain, not one segment (the paused run below
  ends with `iterations: 2` after a single pause).

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0006: awaiting_input + awaiting_until
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
```

The agent pins its model *and* its credential — env vars only fill
provider-less defaults (D28 gotcha: `openai_compatible` without a
`base_url` resolves to `https://api.openai.com/v1`, which then reads as
an "Incorrect API key" failure even though the platform env was right):

```yaml
# /tmp/approval-agent.yaml
name: approval-agent
description: Pauses for tool approval before executing the calculator.
model:
  provider: openai_compatible
  model: gemma4:31b
  base_url: https://ollama.com/v1        # pin the endpoint (D28 gotcha)
  credential_ref:                        # a reference, never the key itself
    type: env
    env_var: OLLAMA_API_KEY              # names an env var (ADR 0005)
system_prompt: >-
  You do arithmetic with the calculator tool. Use it for any arithmetic.
strategy:
  type: function_calling
tools:
  - name: calculator
    config:
      requires_approval: true            # the S10 gate (tool annotations)
memory:
  enabled: false
  max_messages: 20
```

```bash
uv run jarvis agent create --file /tmp/approval-agent.yaml
AGENT_ID=$(uv run jarvis agent show approval-agent | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
echo "agent: $AGENT_ID"
```

## 1. The capabilities fact the UI flips on

```bash
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -B2 human_in_the_loop
# → "executions": { "enabled": true, "detail": { "human_in_the_loop": true } }
```

**Proves** the UI-enablement gate: the run console's pause card and the
Executions inbox render only when the backend reports
`executions.detail.human_in_the_loop` — never faked (decision 1.6).

## 2. A blocking run stops at the pause

```bash
curl -s -w '\nHTTP_STATUS:%{http_code}\n' -X POST localhost:8001/v1/agents/$AGENT_ID/run \
  -H 'Content-Type: application/json' -d '{"input": "what is 40 + 2?"}' --max-time 120 > /tmp/run1.json
# HTTP_STATUS:200
python3 - <<'EOF'
import json
d = json.loads(open('/tmp/run1.json').read().splitlines()[0])
print('status:', d['status'], '| finished_at:', d['finished_at'], '| iterations:', d['iterations'])
print('run_id:', d['run_id'])
EOF
# → status: awaiting_input | finished_at: None | iterations: 0
```

**Proves** a segment end is a 200, not an error — the run row carries
`status: awaiting_input` with no terminal event yet. A client (CLI, web)
that saw a pre-S10 "final" here now knows the run needs a decision.

**Concept (ADR 0004):** pausing is a *runtime decision*: the strategy step
returns tool calls, the approval gate finds a gated subset, and the loop
returns **without `finalize()`** — exactly as cancellation does. The
worker acks the queue row: nothing holds a paused run.

## 3. The durable pause event

```bash
RUN_ID=$(python3 -c "import json;print(json.loads(open('/tmp/run1.json').read().splitlines()[0])['run_id'])")
curl -s localhost:8001/v1/executions/$RUN_ID/events | python3 -c "
import json,sys
d = json.load(sys.stdin)
evs = d['events']
seqs = [e['event']['sequence'] for e in evs]
print('gapless:', seqs == list(range(len(seqs))))
for e in evs:
    ev = e['event']
    if ev['type'] in ('tool.call.requested','run.awaiting_input','run.cancelled'):
        print(e['cursor'], ev['type'], {k: ev[k] for k in ('name','arguments','reason','pending_calls','awaiting_until') if k in ev})
"
# → tool.call.requested  {'name': 'calculator', 'arguments': {'expression': '40 + 2'}}
# → run.awaiting_input   {'reason': 'tool_approval', 'pending_calls': [{...calculator...}],
#                         'awaiting_until': '2026-09-07T…'}   (24h default)
```

**Proves** terminal-adjacency in the wild: `tool.call.requested` fired
(the model asked), but **no `tool.call.started`** — the call never ran
before the human answered. The pending batch rides the durable pause
event, so any process (and the reaper) can see exactly what is waiting.

## 4. Approve → the run completes; a late resume 409s

```bash
curl -s -w '\nHTTP_STATUS:%{http_code}\n' -X POST localhost:8001/v1/executions/$RUN_ID/resume \
  -H 'Content-Type: application/json' -d '{"tool_approval": true}' --max-time 120 > /tmp/resume1.json
# HTTP_STATUS:200
python3 -c "
import json
d = json.loads(open('/tmp/resume1.json').read().splitlines()[0])
print('status:', d['status'], '| final:', d['final_message'], '| iterations:', d['iterations'])"
# → status: succeeded | final: 40 + 2 = 42 | iterations: 2

curl -s -o /dev/null -w 'second resume: %{http_code}\n' -X POST localhost:8001/v1/executions/$RUN_ID/resume \
  -H 'Content-Type: application/json' -d '{"content": "hi"}'
# → 409 — the run is no longer awaiting
```

**Proves** D32: the resume route re-enters the *same* run — history is
already persisted, so the runtime appends the decision, re-invokes the
strategy, and the model executes the approved call in a new iteration.
`iterations: 2` shows the iteration counter spans segments. The resume is
**blocking** (mirrors `/run`): it returns the row for the resumed
segment's end (which may pause again).

The body is validated to exactly one answer: `{"tool_approval": true}`
or `{"content": "..."}` — both is a 422, `{}` is a 422, blank content is
a 422 (the route can't guess what the human meant).

## 5. Reject → the loop continues with a refusal

```bash
curl -s -X POST localhost:8001/v1/agents/$AGENT_ID/run \
  -H 'Content-Type: application/json' -d '{"input": "what is 12 * 12?"}' --max-time 120 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['run_id'], d['status'])"
RUN2_ID=<from above>
curl -s -w '\nHTTP_STATUS:%{http_code}\n' -X POST localhost:8001/v1/executions/$RUN2_ID/resume \
  -H 'Content-Type: application/json' -d '{"tool_approval": false}' --max-time 120
# → status: succeeded | final: 12 * 12 is 144.
```

**Proves** rejection is a *refusal*, not a failure: the declined call is
closed with a `role="tool"` message (`"user declined execution"`) in the
transcript — no `tool.call.started/completed` events, it never ran — and
the model answers from its own knowledge (12×12 is arithmetic a model
can do unaided). The run *completes*: the human's decision is just more
conversation context to the strategy.

## 6. Last-Event-ID across the pause

```bash
curl -s -N -X POST localhost:8001/v1/agents/$AGENT_ID/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"input": "what is 9 * 9?"}' --max-time 60 > /tmp/stream1.txt
grep '^event:' /tmp/stream1.txt
# → run.started … tool.call.requested, run.awaiting_input  (the stream ENDS at the pause)
PAUSE_CURSOR=$(grep -B1 'run.awaiting_input' /tmp/stream1.txt | grep '^id:' | cut -d' ' -f2)
RUN3_ID=<run_id from the run.started frame>

curl -s -o /dev/null -w 'resume: %{http_code}\n' -X POST localhost:8001/v1/executions/$RUN3_ID/resume \
  -H 'Content-Type: application/json' -d '{"tool_approval": true}' --max-time 120

# re-attach beyond the durable pause cursor — ONLY the resumed segment replays:
curl -s -N -X POST localhost:8001/v1/agents/$AGENT_ID/stream \
  -H "Last-Event-ID: $PAUSE_CURSOR" -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"input": "what is 9 * 9?", "run_id": "'$RUN3_ID'"}' --max-time 60 | grep '^event:' | head -20
# → tool.call.started, tool.call.completed, …, run.completed   (cursors all > PAUSE_CURSOR)
```

**Proves** the SSE contract (S10 §4): a pause ends the stream exactly
like a terminal (the web console parks instead of reconnecting), and the
re-attach attaches *beyond* the pause cursor — the already-durable pause
is not replayed and cannot end the stream immediately. Live and replayed
events share one cursor space (the durable `execution_events.cursor`,
ADR 0003).

## 7. Cancelling a paused run is immediate

```bash
RUN4_ID=<pause another run per §2>
curl -s -w '\nHTTP_STATUS:%{http_code}\n' -X POST localhost:8001/v1/executions/$RUN4_ID/cancel
# → {"run_id":"…","cancelled":true,"status":"cancelled"}
curl -s localhost:8001/v1/executions/$RUN4_ID/events | python3 -c "
import json,sys
d = json.load(sys.stdin)
evs = d['events']
seqs = [e['event']['sequence'] for e in evs]
print('gapless:', seqs == list(range(len(seqs))),
      '| last:', evs[-1]['event']['type'], evs[-1]['event'].get('reason'))"
# → run.cancelled {'reason': 'cancelled by user'}  — appended right after the pause
```

**Proves** a paused run is acked (D31): no heartbeat holds it, so cancel
does not wait for a cooperative checkpoint — the cancel route finishes
the run directly (`finish_paused_run`), exactly one terminal from the
paused row. Cancelling an already-cancelled run stays idempotent.

## 8. The pause deadline and the reaper (D32/ADR 0010 §6)

A pause must never strand a run. Give it a 3s deadline and let the
worker's 30s sweeper find it. **Use a single-worker deployment for this
scenario**: both backends share the dev DB, and the pause deadline is
stamped by *whichever worker executes the run* — a leftover `jarvis
serve` on :8001 (default 24h) will claim the message first and the
`JARVIS_AWAITING_INPUT_TIMEOUT_SECONDS=3` on :8002 never applies.

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN -t | xargs kill   # stop the other claimer
JARVIS_PORT=8002 JARVIS_AWAITING_INPUT_TIMEOUT_SECONDS=3 uv run jarvis serve &
curl -s -X POST localhost:8002/v1/agents/$AGENT_ID/run \
  -H 'Content-Type: application/json' -d '{"input": "what is 8 * 8?"}' --max-time 60 \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['run_id'], d['status'])"
RUN5_ID=<from above>

# the row carries the deadline:
psql "$JARVIS_DATABASE_URL" -c "select status, awaiting_until from agent_executions where id='$RUN5_ID'"
# → awaiting_input | now() + 3s

# …wait out the sweeper (≤30s after the deadline), then:
curl -s localhost:8002/v1/executions/$RUN5_ID/events | python3 -c "
import json,sys
d = json.load(sys.stdin)
evs = d['events']
seqs = [e['event']['sequence'] for e in evs]
last = evs[-1]['event']
print('gapless:', seqs == list(range(len(seqs))), '| last:', last['type'], '| reason:', last.get('reason'))"
# → run.cancelled {'reason': 'awaiting_input timeout'} — usage carried, gapless

curl -s -o /dev/null -w 'late resume: %{http_code}\n' -X POST localhost:8002/v1/executions/$RUN5_ID/resume \
  -H 'Content-Type: application/json' -d '{"tool_approval": true}'
# → 409 — and the event log is unchanged (a raced resume is absorbed)
```

**Proves** the no-stuck-run guarantee: an expired pause gets its one
terminal from the sweeper (`finish_paused_run`, same path the cancel
route uses), the queue is acked so a pending resume message is dropped,
and a late human answer reads the row and 409s — the run already ended.

## 9. Strategy-asked pauses (the other trigger class)

`AskHumanStep` lets a strategy stop and ask. Builtin strategies never
ask, so this scenario is covered by a scripted strategy in the
integration suite
(`tests/integration/test_api_run.py::test_resume_content_answer_completes`):
the run pauses with `reason: "strategy"` and a `question`, and
`POST /resume` with `{"content": "Harshit"}` appends the answer as a user
message, re-invokes the strategy inside the paused iteration
(`entry="answer"` — no replayed `iteration.started`), and completes.
There is no plugin seam yet (S3) to register a live ask-strategy — the
route and the runtime path are the same either way.

## 10. Teardown and the web UI

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN -t | xargs kill
lsof -nP -iTCP:8002 -sTCP:LISTEN -t | xargs kill
cd web && npm run dev                  # → http://localhost:5173
```

- **Run console** (`/agents/<id>/run`): a pause renders a violet card —
  approval rows (Approve/Reject) for gated calls, an answer form for a
  question; the resume POST is blocking and the stream re-attaches at the
  pause cursor, replaying the resumed segment into the same timeline.
- **Executions**: an "Awaiting input" inbox lists paused runs (from
  `GET /v1/executions?status=awaiting_input`), above the table.

## Appendix — copy-paste-safe shell (the S2 lesson)

- Every mutating call shows its status (`-w '\n%{http_code}\n'` or
  `tee`-to-file + parse); nothing is fire-and-forget — a silently failed
  409/422 would otherwise read as success and poison every later step.
- Every id is captured from a previous response into a shell variable;
  never copy a run id across steps by hand.
- Key material never appears in output: the credential is referenced by
  the *name* of an env var (`OLLAMA_API_KEY`), whose value lives only in
  the gitignored `.env` (ADR 0005/D29). Any error body that echoes key
  material is a bug to fix, not output to share — mask it.
- `--max-time` on every curl that blocks on a model: a hung provider
  should time the call out, not the reader's patience.