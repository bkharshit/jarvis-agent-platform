# Demo script — JARVIS in under three minutes

A click path for a 2–3 minute screen recording. It shows four things a
reviewer cannot see from the code alone: a streaming run, a human approval
pause and resume, one trace spanning a supervisor and its sub-agent, and a
run fired by a signed webhook.

Every step is a real run. The mock provider cannot call tools, so the
recording needs a **tool-calling model** on any OpenAI-compatible endpoint
(OpenAI, Ollama, vLLM).

## Before recording (off camera, ~10 minutes)

1. Start the stack: `uv run jarvis serve` (API on :8000) and
   `cd web && npm run dev` (UI on :5173). Open `http://localhost:5173`.
2. **Agents.** In **Agents → New agent**, create three agents on the
   tool-calling model, strategy `function_calling`:
   - `calc-assistant` — tools `calculator` and `current_time`. On the
     `calculator` binding set the JSON config to `{"requires_approval": true}`.
   - `research-worker` — tools `calculator` and `current_time`, no approval.
     System prompt: "Answer the question you are given concisely."
   - `supervisor` — one binding `sub__research-worker` pointing at the
     worker. System prompt: "Delegate factual or arithmetic sub-questions to
     the sub__research-worker tool, then combine the answers."
3. **Trigger.** In **Triggers → Create trigger**, kind `webhook`, agent
   `research-worker` (no approval gate, so the fired run finishes on its
   own). Input template: `Alert received: {{payload}} — answer the question
   in it.` (without one, the raw body is the run input). Copy the secret (shown once) and the webhook URL into a
   terminal:
   ```bash
   export SECRET='whsec_…'  WEBHOOK_URL='http://localhost:5173/v1/triggers/webhook/…'
   ```
4. Dry-run every scene once, then clear nothing: earlier runs make the
   **Executions** and **Observability** lists look lived-in.

Window layout: browser on the left two thirds, terminal on the right third
(used only in scene 4).

## Scene 1 — Streaming run (0:00–0:35)

1. **Agents → calc-assistant.** The studio opens with the config pane and
   the run dock.
2. Run input: `What time is it in UTC right now?` → **Run**.
3. Point at the dock as text streams in token by token, then at the run
   timeline: `run.started`, the iteration, the `current_time` tool call,
   and exactly one terminal event.

> Say: "Every run is an ordered event log in Postgres. The UI is just an
> SSE subscriber — a dropped connection resumes from the last event id
> without losing or repeating anything."

## Scene 2 — Approval pause and resume (0:35–1:15)

1. Same agent. Run input: `What is 1234 * 5678, plus 91?` → **Run**.
2. The model calls `calculator`; the run **pauses** and the approval card
   appears with the pending call and its arguments.
3. Hover the card for a beat — the run is paused, not blocking a worker.
4. Click **Approve**. The run resumes, executes the call, and finishes
   with the answer (7006743).

> Say: "A pause is a segment, not a terminal state. The worker is released
> while a human decides; resume goes back through the same queue, and
> decisions are per call — anything not approved is declined, never run."

## Scene 3 — Sub-agent trace waterfall (1:15–2:05)

1. **Agents → supervisor.** Run input:
   `What is 17% of 2,350, and what is today's date?` → **Run**.
2. The supervisor calls `sub__research-worker`; the parent tool call blocks
   while a child run executes.
3. Open the finished run in **Executions** and show its child run listed
   under it.
4. **Observability** → open the newest trace. The waterfall shows one trace
   across both runs: the parent's segment and iterations, the LLM calls, the
   sub-agent tool call, and the child's own spans nested inside its window.

> Say: "Children are ordinary runs with their own event logs, but they
> inherit the parent's trace id and deadline — so one waterfall explains
> the whole conversation. The same spans can export over OTLP."

## Scene 4 — Webhook-fired run (2:05–2:45)

1. In the terminal, sign a body and post it:
   ```bash
   BODY='{"alert":"Disk at 91% on db-1. What is 91% of 500 GB?"}'
   SIG="sha256=$(printf %s "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
   curl -s -X POST "$WEBHOOK_URL" -H "X-Jarvis-Signature: $SIG" \
        -H 'Content-Type: application/json' --data-binary "$BODY"
   ```
   The ingest answers `202` with the new run id — it enqueues and returns
   without waiting for the run.
2. Show a bad signature being rejected: rerun with `SIG=sha256=00` → `401`.
3. Back in the browser, **Triggers** → the webhook trigger's runs link. The
   fired run is listed; open it to show the alert text as its input and the
   worker's answer (455 GB).

> Say: "Triggers fire ordinary queued runs. The webhook is the one
> unauthenticated route — the HMAC signature is the authentication, and
> the secret is shown exactly once."

## Close (2:45–3:00)

Cut to the README's architecture diagram.

> Say: "One runtime owns every limit and guarantees exactly one terminal
> state per run. Workflows, RAG, evaluations, and triggers are built on that
> same seam. Architecture decisions are in docs/adr."

## If something goes wrong on camera

- **The model answers without calling the tool.** Make the input demand
  it ("Use the calculator to …"); smaller models skip tools on easy math.
- **The supervisor answers itself instead of delegating.** Add "Always use
  the sub__research-worker tool" to its system prompt.
- **The webhook returns 401 with the right secret.** The body was changed
  after signing — sign and post the same `$BODY` variable.
