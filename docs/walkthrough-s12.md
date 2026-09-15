# S12 manual walkthrough — richer memory (ADR 0016), live script

A hands-on reproduction of the S12 acceptance behavior against a live
stack. Format follows walkthrough-s6/s4. Backend :8001, web :5173, dev
DB `jarvis` (migration 0010), mock agents for the deterministic steps,
real `gemma4:31b` for the human-readable scratchpad steps.

S12 in one sentence: **conversation memory gains a strategy** — `window`
(exactly the pre-S12 flat last-N slice, the default for every existing
snapshot) or `summarize` (the evicted prefix is compacted into a rolling
conversation summary through the run's own client), plus a per-session
**scratchpad** the model drives through three new builtin tools
(`memory_get` / `memory_put` / `memory_delete`, D46).

The three invariants the walkthrough exercises:

- **D45 — memory never fails a run.** A summarizer model error degrades
  the segment to the plain window (whatever summary state already exists
  is kept); cancellation still propagates. A resumed segment rebuilds
  its view READ-ONLY — existing summary + window, no compaction call.
- **D46 — the scratchpad is a tool surface, not memory.** It keys on
  (agent_id, session_id) from the run context and is bindable by any
  agent, memory on or off. A run without a session fails honestly
  (is_error "requires a session"), never an exception past the runtime.
- **Tenant discipline (D29)**: scratchpad rows are tenant-scoped like
  every store — a foreign tenant reads absence, never the row.

## 0. Setup

The backend must be restarted to pick up the S12 registry (the memory_*
builtins register at boot) and migration 0010:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0010: memory_scratch
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

auth_mode is `required` in `.env` — reuse your session identity (or
provision one, walkthrough-s6 §0 pattern), then:

```bash
export JARVIS_KEY="jarvis_sk_…"       # your existing api key
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity — the memory builtins appear in the registry-derived tool list:

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tools"])'
# → builtins include memory_get, memory_put, memory_delete
```

## 1. The summarize agent — compaction consumes a turn (mock, deterministic)

Create a memory agent with a TIGHT window so compaction fires fast
(mock provider + function_calling — the D19 smoke rule):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "summarize-demo",
  "description": "S12 compaction demo",
  "model": {"provider": "mock", "model": "mock-1"},
  "strategy": {"type": "function_calling"},
  "memory": {"enabled": true, "max_messages": 2, "strategy": "summarize"}
}' | python3 -m json.tool | grep -E '"id"|strategy'
export SUM_AGENT="<id from above>"
```

Run four turns in ONE session (history grows past the window=2):

```bash
for i in 1 2 3 4; do
  curl -s -H "$AUTH" -H "Content-Type: application/json" \
    localhost:8001/v1/agents/$SUM_AGENT/run \
    -d "{\"input\": \"message number $i\", \"session_id\": \"s-demo\"}" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["status"], r["run_id"])'
done
```

Receipt 1 — the DB carries the rolling state (the default reply
"This is a mock response." IS the summary; the mock cannot write prose,
the machinery is the point):

```bash
psql "postgresql://jarvis:jarvis@localhost/jarvis" \
  -c "SELECT summary, summarized_count FROM conversations ORDER BY updated_at DESC LIMIT 1"
# → summary = This is a mock response. / summarized_count > 0
```

Receipt 2 — the compaction call is visible as an EXTRA model call. With
`JARVIS_LLM_TRACE=true` (already in `.env`), take a run id from a later
turn and open the trace:

```bash
curl -s -H "$AUTH" localhost:8001/v1/executions/<run_id>/llm-trace | python3 -m json.tool
# one call's request messages start with the summarizer system prompt
# ("You compress conversation history…") — that is the compaction call,
# followed by the loop call carrying the window
```

## 2. The prompt view — summary ahead of the window

Run one more turn in the same session and inspect the loop request in
the llm-trace: the message list is [system, "Summary of the earlier
conversation:\n…", ≤2 window messages, the new user input]. The evicted
prefix appears nowhere verbatim.

## 3. Web — the editor Strategy select

Open http://localhost:5173 → Agents → summarize-demo (or any agent):
the Memory panel has a **Strategy** select (window / summarize) beside
Max messages and Session key. Switching summarize-demo to `window` and
saving publishes a new version; the detail page's Memory line names the
strategy (`enabled, max 2 messages, summarize strategy`). Existing
agents (pre-S12 snapshots) draft as `window`.

## 4. The scratchpad — a real model stores and recalls

Create an agent with the three memory tools bound, pinned to gemma:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "scratchpad-demo",
  "description": "S12 scratchpad demo",
  "model": {"provider": "openai_compatible", "model": "gemma4:31b",
            "base_url": "https://ollama.com/v1",
            "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}},
  "strategy": {"type": "function_calling"},
  "tools": [{"name": "memory_get"}, {"name": "memory_put"}, {"name": "memory_delete"}]
}' | python3 -m json.tool | grep '"id"'
export SCR_AGENT="<id>"
```

Store then recall, SAME session:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$SCR_AGENT/run \
  -d '{"input": "Remember for this session: my project codename is NIGHTINGALE.", "session_id": "s-scratch"}'
# → run succeeds; the console shows a memory_put tool call

curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$SCR_AGENT/run \
  -d '{"input": "What is my project codename? Check your memory tool.", "session_id": "s-scratch"}'
# → "NIGHTINGALE" via a memory_get call
```

Receipts: the tool calls are ordinary — visible as `tool_executions`
rows and in the run console's iteration; nothing about the scratchpad
is special on the wire. A different session must NOT recall it:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$SCR_AGENT/run \
  -d '{"input": "What is my project codename? Check your memory tool.", "session_id": "s-other"}'
# → memory_get errors honestly ("not set") and the model says so
```

## 5. No session → honest is_error

Run scratchpad-demo WITHOUT a session_id and ask it to store something:
the memory_put tool result is an is_error "the scratchpad requires a
session" the model narrates — the run still completes (the recoverable
ToolResult path; D46). Memory stays off/on-independent — note
summarize-demo has NO memory tools and scratchpad-demo has NO memory
config; the two surfaces are orthogonal.

## 6. What is unit-covered, not live-scripted

- **Degrade (D45)**: a summarizer ModelError degrades to the window,
  state untouched, run succeeds — `TestSummarizerFailureDegrades…` and
  the runtime-level `test_summarizer_failure_degrades_to_window`.
  Triggering it live needs a mid-run provider outage; not worth faking.
- **Resume windowing**: a resumed segment rebuilds read-only (summary +
  window, zero extra model calls) — `test_resume_rebuilds_read_only…`.
  If you want the live flavor: pair this walkthrough's §1 agent with the
  S10 walkthrough's gated-tool pause and confirm the resumed segment's
  llm-trace shows no compaction call.

## 7. Teardown / receipts

```bash
psql "postgresql://jarvis:jarvis@localhost/jarvis" \
  -c "SELECT key, left(value, 40), tenant_id FROM memory_scratch WHERE session_id LIKE 's-%'"
# the scratchpad rows from §4/§5 (agent-scoped, default tenant)
curl -s -H "$AUTH" localhost:8001/v1/agents | python3 -m json.tool | grep '"name"'
# summarize-demo + scratchpad-demo remain for the session; delete if unwanted
```

## 8. Session notes — findings from the live run (2026-09-15)

All sections ran green. Two session finds worth keeping:

1. **Cross-run scratchpad recall requires KEY AGREEMENT.** §4's recall
   run first failed honestly: run 1 stored under the key the MODEL
   chose (`project_codename`), run 2 guessed `project codename` (with
   a space) — memory_get errored "not set" and the model said so. Not
   a bug (the scratchpad is a tool surface, D46 — keys are the
   caller's contract), but the demo needed the key NAMED in both
   prompts ("store under the key `project_codename`" / "read the key
   `project_codename`"). Future nicety, not obligation: a
   key-listing memory tool would make discovery model-driven.
2. **Compaction receipts, live**: the turn-4 llm-trace shows the
   summarizer call ("You compress conversation history…") followed by
   the loop call carrying the summary system message; the evicted
   prefix appears nowhere verbatim; `summarized_count` = 4. The web
   Strategy select (§3) was exercised by Harshit directly — switching
   summarize-demo to `window` published a new version, and the S11
   eval runs against it pinned that version (the two stages share the
   demo agent).