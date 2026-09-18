# S13 manual walkthrough — triggers (ADR 0019), live script

A hands-on reproduction of the S13 acceptance behavior against a live
stack. Format follows walkthrough-s12/s6. Backend :8001, web :5173, dev
DB `jarvis` (migration 0013), mock agents for the deterministic steps.

S13 in one sentence: **triggers start runs on their own** — three kinds
in v1: `cron` (a five-field croniter schedule, UTC), `webhook` (the ONE
unauthenticated ingest route, HMAC-SHA256-verified), and `event` (a
watcher on run outcomes via the finalize observer, D61), all firing
ordinary runs through the same worker path a POST /v1/agents/{id}/run
uses, with provenance (`metadata.trigger`) that the executions list
filters on.

The invariants the walkthrough exercises:

- **D59 — one scheduler, own Postgres loop.** A single `fire_due()`
  scan claims due cron rows with `SKIP LOCKED` — concurrent claimants
  can't double-fire, and a backlogged schedule catches up as ONE fire
  (advance from now), never a burst.
- **D60 — the trigger row is the trust boundary.** The webhook route is
  the one unauthenticated ingest: missing/wrong signature → 401, a
  disabled trigger behaves as absent (404, no existence leak), the
  plaintext secret exists EXACTLY ONCE (create / rotate) and never in a
  GET or a log.
- **D61 — the depth guard stops event chains.** A trigger-fired run
  carries `metadata.trigger.depth = incoming + 1`; the finalize observer
  refuses to fire when the finalized run's depth is already at
  `trigger_max_depth` (default 1) — a run loop is impossible by
  construction. The guard reads the PERSISTED row, not the caller's
  metadata.
- **Firing fails honestly.** A trigger whose target agent vanished (or
  has no published version) does not crash the scheduler — it stamps
  `last_error` on the row, `fire_due` counts only successful fires, and
  the claim already advanced `next_fire_at` (the next slot is the retry,
  never a storm).

## 0. Setup

The backend must be restarted to pick up the S13 registry (routes +
scheduler) and migration 0013:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0013: triggers
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

auth_mode is `required` in `.env` — reuse your session identity:

```bash
export JARVIS_KEY="jarvis_sk_…"       # your existing api key
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity — the triggers capability is live and the scheduler is polling
(`trigger_poll_seconds` default 5s; it logs nothing when idle):

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sections"]["triggers"])'
# → {"enabled": true, "summary": …, "detail": {"total": 0, "cron": 0, …}}
```

## 1. Cron — the nightly digest (mock, deterministic)

Create the target agent and a cron trigger firing at the top of every
hour (soon enough to watch live; the schedule string is validated by
croniter at the boundary — a bad one 422s):

```bash
curl -s -H "$AUTH" localhost:8001/v1/agents -H 'Content-Type: application/json' -d '{
  "name": "demo-cron", "model": {"provider": "mock", "model": "mock-model"},
  "strategy": {"type": "function_calling"}
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
# → AGENT_ID
curl -s -H "$AUTH" localhost:8001/v1/triggers -H 'Content-Type: application/json' -d "{
  \"name\": \"hourly-tick\", \"kind\": \"cron\", \"agent_id\": \"$AGENT_ID\",
  \"version_policy\": \"latest\",
  \"input_template\": \"Hourly tick at {{now}}.\",
  \"config\": {\"type\": \"cron\", \"schedule\": \"0 * * * *\"}
}" | python3 -m json.tool
# → 201; note id, next_fire_at (the first crontab slot after now)
```

Watch it fire through the scheduler loop (no manual fire — the poll
loop does the work; wait for the next `:00` slot + a few seconds):

```bash
sleep 90
curl -s -H "$AUTH" "localhost:8001/v1/executions?trigger_id=$TRIGGER_ID" \
  | python3 -c 'import json,sys; [print(r["run_id"], r["status"], r["final_message"]) for r in json.load(sys.stdin)["items"]]'
# → one run, succeeded, "… at <iso timestamp>." (the {{now}} substitution)
curl -s -H "$AUTH" localhost:8001/v1/triggers/$TRIGGER_ID \
  | python3 -c 'import json,sys; t=json.load(sys.stdin)["trigger"]; print(t["last_fired_at"], t["next_fire_at"], t["last_error"])'
# → timestamp set, next slot an hour out, last_error None
```

**Catch-up collapse (D59).** Backdate the row's `next_fire_at` an hour
behind, then watch ONE fire, not a burst — the advance is from now:

```bash
psql jarvis -c "UPDATE triggers SET next_fire_at = now() - interval '90 minutes' WHERE id = '$TRIGGER_ID'"
sleep 8   # one scheduler poll
curl -s -H "$AUTH" "localhost:8001/v1/executions?trigger_id=$TRIGGER_ID" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["items"]))'
# → runs before + 1 (exactly one new run for 90 minutes of backlog)
```

**Honest failure.** Point the trigger at an agent, then delete the
agent. The next slot fires nothing, stamps `last_error`, and does not
crash the loop:

```bash
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  localhost:8001/v1/triggers/$TRIGGER_ID \
  -d "{\"name\": \"hourly-tick\", \"kind\": \"cron\", \"agent_id\": \"agent-gone\",
       \"version_policy\": \"latest\", \"enabled\": true,
       \"config\": {\"type\": \"cron\", \"schedule\": \"0 * * * *\"}}"
# → 200 (agent_id is a loose reference — no FK, no 404 at write time)
psql jarvis -c "UPDATE triggers SET next_fire_at = now() - interval '1 minute' WHERE id = '$TRIGGER_ID'"
sleep 8
curl -s -H "$AUTH" localhost:8001/v1/triggers/$TRIGGER_ID \
  | python3 -c 'import json,sys; t=json.load(sys.stdin)["trigger"]; print(t["last_error"])'
# → "… not found …" — and the scheduler is still alive (healthz 200)
```

## 2. Webhook — signed ingest, secret shown once (D60)

Create a webhook trigger — the response carries the plaintext secret
EXACTLY ONCE:

```bash
curl -s -H "$AUTH" localhost:8001/v1/triggers -H 'Content-Type: application/json' -d "{
  \"name\": \"alerts-hook\", \"kind\": \"webhook\", \"agent_id\": \"$AGENT_ID\",
  \"version_policy\": \"latest\", \"config\": {\"type\": \"webhook\"}
}" | python3 -c 'import json,sys; b=json.load(sys.stdin); open("/tmp/whsec","w").write(b["secret"]); print(b["trigger"]["id"])'
# → WEBHOOK_ID; the secret file holds whsec_… — a GET never returns it:
curl -s -H "$AUTH" localhost:8001/v1/triggers/$WEBHOOK_ID | grep -c whsec   # → 0
```

Sign a body with HMAC-SHA256 and post it — the ingest is
UNAUTHENTICATED (no $AUTH header) and never blocks on the run:

```bash
BODY='{"alert":"disk 91% on db-1"}'
SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$(cat /tmp/whsec)" | awk '{print $NF}')"
curl -s -D- -o /tmp/ingest.json -X POST localhost:8001/v1/triggers/webhook/$WEBHOOK_ID \
  -H "X-Jarvis-Signature: $SIG" -H 'Content-Type: application/json' --data-binary "$BODY"
# → 202 {"run_id": "…", "status": "queued"} — then check the run picked the payload up:
sleep 3; RUN_ID=$(python3 -c 'import json; print(json.load(open("/tmp/ingest.json"))["run_id"])')
curl -s -H "$AUTH" localhost:8001/v1/executions/$RUN_ID \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["run"]; print(r["status"], r["input"])'
# → succeeded, input embeds the raw alert JSON (the {{payload}} default template)
```

The honesty matrix, one curl each:

```bash
# wrong signature → 401 invalid_signature (constant-time compare)
curl -s -X POST localhost:8001/v1/triggers/webhook/$WEBHOOK_ID \
  -H "X-Jarvis-Signature: sha256=deadbeef" --data-binary "$BODY"
# missing header → 401; oversized body → 413:
head -c 70000 /dev/zero | curl -s -X POST localhost:8001/v1/triggers/webhook/$WEBHOOK_ID \
  -H "X-Jarvis-Signature: $SIG" --data-binary @-
# disabled trigger behaves as ABSENT (404, not 401/409 — no existence leak):
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' \
  localhost:8001/v1/triggers/$WEBHOOK_ID \
  -d "{\"name\": \"alerts-hook\", \"kind\": \"webhook\", \"agent_id\": \"$AGENT_ID\",
       \"version_policy\": \"latest\", \"enabled\": false, \"config\": {\"type\": \"webhook\"}}"
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/v1/triggers/webhook/$WEBHOOK_ID \
  -H "X-Jarvis-Signature: $SIG" --data-binary "$BODY"    # → 404
# re-enable before moving on (same full-document PATCH with enabled true)
```

**Rotate** — the new plaintext also exists exactly once; the old secret
stops verifying immediately:

```bash
curl -s -X POST -H "$AUTH" localhost:8001/v1/triggers/$WEBHOOK_ID/rotate-secret \
  | python3 -c 'import json,sys; open("/tmp/whsec","w").write(json.load(sys.stdin)["secret"])'
# repeat the signed POST → 202 again; the OLD signature → 401
```

## 3. Event — outcome watchers and the depth guard (D61)

First make a run that fails DETERMINISTICALLY — the receipt the
watcher will observe. **The mock provider never resolves
`credential_ref`**: a mock agent with a missing env var SUCCEEDS (found
live — the session's first attempt produced a green run; see §6). The
recipe that fails every time is `openai_compatible` pointed at a dead
endpoint — model resolution is inside the runtime's try block (D28), so
the connect failure is a persisted terminal state, never an exception:

```bash
curl -s -H "$AUTH" localhost:8001/v1/agents -H 'Content-Type: application/json' -d '{
  "name": "doomed-agent", "model": {"provider": "openai_compatible", "model": "gemma4:31b",
    "base_url": "http://127.0.0.1:9/v1",
    "credential_ref": {"type": "env", "env_var": "JARVIS_DEFINITELY_MISSING_KEY"}},
  "strategy": {"type": "function_calling"}
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
# → DOOMED_ID
```

Then the event trigger watching that agent's failures — it FIRES the
demo agent (`agent_id`); the watcher's `source_agent_id` is the doomed
one:

```bash
curl -s -H "$AUTH" localhost:8001/v1/triggers -H 'Content-Type: application/json' -d "{
  \"name\": \"on-failure\", \"kind\": \"event\", \"agent_id\": \"$AGENT_ID\",
  \"version_policy\": \"latest\",
  \"input_template\": \"A run finished with status {{status}}. Output: {{output}}\",
  \"config\": {\"type\": \"event\", \"status\": \"failed\", \"source_agent_id\": \"$DOOMED_ID\"}
}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["trigger"]["id"])'
# → EVENT_ID
```

Now fail a run and watch the finalize observer (which runs AFTER the
run's commit) fire the watcher:

```bash
curl -s -H "$AUTH" localhost:8001/v1/agents/$DOOMED_ID/run \
  -H 'Content-Type: application/json' -d '{"input": "anything", "session_id": null}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])'
# → SRC_RUN_ID; GET it → status "failed" — error_kind "strategy", message
#    "strategy 'function_calling' raised ConnectError: All connection attempts failed"
#    (the strategy wraps the model's connect error)
sleep 3
curl -s -H "$AUTH" "localhost:8001/v1/executions?trigger_id=$EVENT_ID" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["items"][0]; print(r["status"], r["metadata"]["trigger"])'
# → succeeded  {"trigger_id": EVENT_ID, "kind": "event", "name": "on-failure", "depth": 1}
#    (parent_run_id is None — the source is linked by provenance, not parenting)
```

**The depth guard — a chain is impossible by construction.** The fired
run itself reached terminal, and the observer saw it — but its depth is
already at `trigger_max_depth` (1), so nothing re-fires. The executions
listing for EVENT_ID gains NO run beyond the one above, however long
you wait. (Set `JARVIS_TRIGGER_MAX_DEPTH=2` in `.env`, restart, and the
same exercise produces depth-2 runs — then put it back.)

**Row-metadata authority.** The guard reads the PERSISTED row: a caller
that POSTs a run with forged `metadata.trigger.depth = 9` still fires
the watcher (the row has no such metadata), and a run whose row carries
`depth: 1` is refused even if the caller claims `{}`. This is
regression-tested (`test_row_metadata_authority`); on the live stack it
is observable only through the code path.

## 4. Fire-now, provenance browsing, and the UI

Fire-now (admin/owner; 202, run queued — the webhook secret UI
discipline applies to cron/event too: no secret, no rotate button):

```bash
curl -s -X POST -H "$AUTH" localhost:8001/v1/triggers/$TRIGGER_ID/fire
# → 202 {"run_id": …}
```

Then the web shell (http://localhost:5173), in order:

1. **Triggers** (nav) — both rows render; the webhook row derives its
   ingest URL client-side; `last_error` rows show a red alert line.
2. **Add trigger** → pick kind `webhook` → Create → the secret banner
   appears with the note that this is the only time it is shown →
   **Dismiss** → it is gone and no GET brings it back.
3. **Fire now** on the cron row → toast "Fired … — run queued".
4. **Runs** link on a row → `/executions?trigger=<id>` → the list is
   filtered; the heading says "— by trigger".
5. Open a trigger-fired run's detail → the **"Fired by"** chip names
   the trigger (kind + depth) and links back to the same filter.
6. A signed-in **member** (role `member`) sees no mutating controls —
   parity with `_require_trigger_manager` at the API.
7. Disable the cron trigger via the row toggle → a full-document PATCH
   leaves; the scheduler stops firing it while history and runs remain
   (delete keeps provenance too — the runs stay filterable).

## 5. Teardown

```bash
curl -s -X DELETE -H "$AUTH" localhost:8001/v1/triggers/$TRIGGER_ID    # 204
curl -s -X DELETE -H "$AUTH" localhost:8001/v1/triggers/$WEBHOOK_ID    # 204
curl -s -X DELETE -H "$AUTH" localhost:8001/v1/triggers/$EVENT_ID      # 204
```

Deleting a trigger keeps the runs it fired (provenance outlives the
rule — the rows stay filterable by `trigger_id`).

## 6. Session notes — findings from the live run

Session ran 2026-09-16 (12:04–13:34 IST), §§1–4 green, with Harshit.
Receipts from the dev DB:

- **Cron (§1):** `hourly-tick` created 12:06, fired the backdated
  catch-up slot ONE poll after the raw-SQL UPDATE (12:07:53 — exactly
  one run, no burst), then kept firing every hour — **60 consecutive
  successful runs through 2026-09-18**, with natural catch-up-once
  after backend downtime (e.g. the 22:30 slot landed at 22:39 after a
  restart, as the next poll's single fire). `last_error` stayed empty;
  the runs' `metadata.trigger` blocks are byte-identical
  `{kind, name, depth: 1, trigger_id}`. The trigger itself is the
  durability receipt — it is still enabled and firing.
- **Webhook (§2):** `alerts-hook` created 13:23; three signed ingests
  all succeeded (13:24:16, 13:27:52, 13:29:21 — signed POST, the
  post-rotate re-POST, and one repeat), each a queued run whose input
  is the raw alert JSON verbatim. The plaintext secret never appeared
  in a GET.
- **Event (§3):** the `on-failure` watcher fired once off the doomed
  agent's terminal failure — fired run succeeded with input
  "A run finished with status failed. Output: " (the failed source
  has no output) and depth-1 provenance; no re-fire ever appeared
  (the depth guard held for two days of subsequent runs).

**Live finding — the §3 recipe was wrong as drafted (doc fixed):** the
mock provider never resolves `credential_ref`, so `doomed-agent`
(mock + missing env var) **succeeded** — a mock agent cannot be made
to fail this way. The working recipe, verified live: `doomed-agent-2`
= `openai_compatible` + dead `base_url` (`http://127.0.0.1:9/v1`) →
ConnectError → persisted terminal failure. Two second-order receipts:
the terminal `error_kind` is **`strategy`**, not `model` (the
strategy wraps the model's connect error — the message names both),
and the first draft also pointed the watcher's `source_agent_id` at
the demo agent itself; live, the trigger was created against
`doomed-agent` and full-doc PATCHed to `doomed-agent-2` — the doc now
creates the doomed agent first and passes `$DOOMED_ID` as the source.

**Teardown (§5) deliberately NOT run:** the demo rows (`hourly-tick`,
`alerts-hook`, `on-failure`, `demo-cron`, `doomed-agent`,
`doomed-agent-2`) stay in the dev DB as live fixtures — Harshit will
return with test cases against them; `hourly-tick` keeps firing in
the meantime.

**Known unreproduced flake (build era):** one integration run failed
once at commit 4 (trigger domain/repo), then the next two consecutive
runs were green — never reproduced, no root cause found. Watch for it
in future integration passes.

**Deferred with rationale (ADR 0019 §8):** IANA timezones (UTC only
in v1), webhook replay protection (nonce/timestamp), "tool emits Y"
event conditions, stable per-trigger sessions, pause notifications
(S10's owed webhooks), per-trigger backpressure.