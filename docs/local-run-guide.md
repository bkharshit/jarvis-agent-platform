# Local Run Guide — hands-on walkthrough (living document)

> **Purpose:** the exact, copy-pasteable sequence for running JARVIS locally
> and exercising every implemented feature — the manual verification script
> for the whole platform. **Update this document as each roadmap stage
> lands**: new endpoints, new CLI commands, and new capabilities get a
> command here the day they work. See the coverage table at the bottom.

**Last verified:** 2026-09-05 · **Backend state:** Phase 1 complete
(all 21 commits) · **Server:** `uv run jarvis serve` on `:8000`

---

## 1. Environment (this machine)

No Docker. Everything runs against local services:

| Component | What | Notes |
| --- | --- | --- |
| Postgres | local install on `:5432` | role `jarvis`/`jarvis`, dbs `jarvis` (dev) + `jarvis_test` (integration tests) |
| Ollama | local on `:11434` | real-model runs; currently `glm-5.3:cloud` |
| Python | `uv` | deps via `uv sync --extra dev` |

```bash
# one-time sanity checks
pg_isready -h localhost -p 5432                 # accepting connections
curl -s localhost:11434/api/tags | python3 -m json.tool | head   # models
```

```bash
# one-time setup (idempotent)
uv sync --extra dev
uv run alembic upgrade head       # migrate the dev db
```

> Never use `make test-db` here — it needs Docker (compose). Integration
> tests talk to local Postgres directly:
> `uv run pytest tests/integration -q -m db`

## 2. Start / stop the server

```bash
uv run jarvis serve               # foreground, Ctrl-C to stop
```

Expected boot log:

```
Serving on http://127.0.0.1:8000
INFO:     Started server process [...]
INFO:     Application startup complete.
```

Nothing else runs — no workers, no sidecars. Modular monolith; Postgres is
the only external dependency. Kill it with Ctrl-C (or
`kill <pid from the log>` / find it via `lsof -i :8000`).

## 3. The walkthrough — every feature, in order

### 3.1 Health

```bash
curl -s localhost:8000/healthz
# {"status":"ok"}
```

### 3.2 Create an agent (real model via Ollama)

`api_key_env` is omitted → no `Authorization` header, which is what Ollama
wants. Creating publishes **version 1** automatically.

```bash
curl -s -X POST localhost:8000/v1/agents -H 'Content-Type: application/json' -d '{
  "name": "hands-on-agent",
  "description": "Demo agent for a live walkthrough",
  "model": {"provider": "openai_compatible", "model": "glm-5.3:cloud",
            "base_url": "http://localhost:11434/v1"},
  "system_prompt": "You are a helpful assistant. Use the calculator tool for any arithmetic.",
  "strategy": {"type": "function_calling"},
  "tools": [{"name": "calculator"}, {"name": "current_time"}],
  "memory": {"enabled": true, "max_messages": 10},
  "max_iterations": 8,
  "temperature": 0.2
}' | python3 -m json.tool
# → 201; note "id" (agent id) and versions: [{version: 1, label: "initial"}]
```

Zero-network variant (no Ollama needed): use `"provider": "mock"` **with**
`"strategy": {"type": "function_calling"}` — the mock's canned reply never
produces a ReAct `Action:`, so react+mock runs to its iteration cap and
fails gracefully by design. Same applies to
`uv run jarvis agent create --file examples/research-agent.yaml` (that file
pins `react` + `openai_compatible`).

### 3.3 Streaming run over SSE — the flagship demo

```bash
AGENT_ID=<id from 3.2>
curl -sN -X POST localhost:8000/v1/agents/$AGENT_ID/stream \
  -H 'Content-Type: application/json' \
  -d '{"input": "What is 144 * 12? Use the calculator tool, then tell me the answer."}'
```

What a healthy trace looks like, in order (each frame is
`id: <cursor>` / `event: <type>` / `data: <json>`):

1. `run.started` — carries `run_id`, `agent_version_id` (the run pinned a
   published version), and the global cursor
2. `iteration.started` → `model.invocation.started`
3. `model.invocation.completed` with `finish_reason: "tool_calls"` — the
   model decided to call a tool
4. `tool.call.requested` → `tool.call.started` → `tool.call.completed`
   (`output: "1728"`, `is_error: false`, `latency_ms: 1`)
5. second `iteration.*`, then `text.delta` frames streaming the answer
6. `run.completed` — **exactly one terminal event**, with
   `final_message`, `total_usage`, `iterations`

Timing check: a healthy tool round-trip is ~1.3 s to first tool call with
Ollama; the calculator itself is ~1 ms.

### 3.4 Execution records — inspect what was persisted

```bash
RUN_ID=<run_id from the run.started frame>

# list runs (filter: agent_id, status, session_id; page: limit, offset)
curl -s "localhost:8000/v1/executions?limit=3" | python3 -m json.tool

# full detail: run row + message transcript + tool executions
curl -s localhost:8000/v1/executions/$RUN_ID | python3 -m json.tool
# ToolResult.output is TOP-LEVEL in JSON (tool_executions[i].output), per D17

# event replay from the DB (JSON; add -H 'Accept: text/event-stream' for SSE)
curl -s "localhost:8000/v1/executions/$RUN_ID/events" | python3 -m json.tool

# cursor-filtered replay — only events after a given cursor
curl -s "localhost:8000/v1/executions/$RUN_ID/events?after=60" | python3 -m json.tool
```

Two-level sequencing to verify in the output: per-run `sequence` starts at
0 and is gapless; global `cursor` never repeats across runs and *is* the
SSE `Last-Event-ID` (ADR 0003). Resume of a live run:
`curl -N -H "Last-Event-ID: <cursor>" ... -d '{"input": ..., "run_id": ...}'`.

### 3.5 Memory across runs (session continuity)

```bash
curl -s -X POST localhost:8000/v1/agents/$AGENT_ID/run -H 'Content-Type: application/json' \
  -d '{"input": "Hi! Remember this number: 42. Just say ok.", "session_id": "demo-session-1"}'

curl -s -X POST localhost:8000/v1/agents/$AGENT_ID/run -H 'Content-Type: application/json' \
  -d '{"input": "What number did I ask you to remember, times 2? Use the calculator.", "session_id": "demo-session-1"}'
# → second run recalls 42, calls calculator, answers 84

curl -s "localhost:8000/v1/conversations/$AGENT_ID/demo-session-1/messages" | python3 -m json.tool
# → full interleaved history: user/assistant/tool messages across both runs
```

### 3.6 Versioning — every update publishes an immutable snapshot

```bash
curl -s -X PATCH localhost:8000/v1/agents/$AGENT_ID -H 'Content-Type: application/json' \
  -d '{"temperature": 0.5}' | python3 -c "import json,sys; print(json.load(sys.stdin)['versions'])"
# → [{'version': 1, ...}, {'version': 2, ...}]; earlier runs still pin v1

curl -s localhost:8000/v1/agents/$AGENT_ID/versions | python3 -m json.tool
```

### 3.7 Error envelope — one shape everywhere

```bash
# 409 conflict (duplicate name)
curl -s -w "  [%{http_code}]\n" -X POST localhost:8000/v1/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "hands-on-agent", "model": {"provider": "mock", "model": "m"},
       "strategy": {"type": "function_calling"}}'
# → {"error":{"kind":"conflict","message":"agent name 'hands-on-agent' already exists"}}

# 404 not found
curl -s -w "  [%{http_code}]\n" \
  -X POST localhost:8000/v1/agents/00000000-0000-0000-0000-000000000000/run \
  -H 'Content-Type: application/json' -d '{"input": "hi"}'
# → {"error":{"kind":"not_found",...}}

# 422 validation (blank name; also fires for missing required fields)
curl -s -w "  [%{http_code}]\n" -X POST localhost:8000/v1/agents \
  -H 'Content-Type: application/json' -d '{"name": ""}'
# → {"error":{"kind":"validation","details":{"errors":[...per-field...]}}}
```

### 3.8 CLI — the same API as a second client

```bash
uv run jarvis --help
uv run jarvis doctor --ping-model
uv run jarvis agent list
uv run jarvis run <agent-name> "2+2?" --stream      # needs a reachable model
uv run jarvis executions list
uv run jarvis executions show $RUN_ID --events       # replay the event log
```

CLI notes: `executions list` has no `--limit` flag (API-only); duplicate
agent names surface as friendly conflicts.

### 3.9 Under the hood — Postgres directly

```bash
psql -U jarvis -h localhost -d jarvis

\d execution_events          -- note: PK on "cursor" (quote it in queries;
                             -- UNIQUE (execution_id, sequence) = gapless per-run)
SELECT "cursor", sequence AS seq, event_type, created_at::timestamp(0)
  FROM execution_events ORDER BY "cursor" DESC LIMIT 6;
SELECT version, label, created_at::timestamp(0) FROM agent_versions ORDER BY version;
```

Tables: `agents`, `agent_versions`, `agent_executions`, `messages`,
`tool_executions`, `execution_events`, `conversations`, `alembic_version`.

## 4. Findings from the 2026-09-05 run-through

Everything implemented in Phase 1 **works end-to-end against real Postgres
and a real Ollama model**:

- ✅ Agents CRUD + append-only versioning; every run pins a published version
- ✅ Streaming run over SSE with real tool calling (function_calling → Ollama)
- ✅ Exactly-once event log: gapless per-run sequence + global cursor as the
  SSE resume token, replayable from the DB after the fact
- ✅ Execution detail: run + transcript + tool executions with latency
- ✅ Cross-run memory within a session, full conversation history endpoint
- ✅ Single error envelope (409/404/422 all verified)
- ✅ CLI as a second client over the same API
- ❌ `GET /v1/capabilities` → 404 (correct: that is F1's deliverable)
- ❌ Auth, MCP, workflows, RAG — not built (S2/S4/S6/S8), as expected

## 5. Coverage — update this table + §3 with each stage

| Stage | Feature | Added to this guide | Verified |
| --- | --- | --- | --- |
| Phase 1 | runtime, API, SSE + resume, persistence, CLI, memory | §3.1–3.9 | 2026-09-05 |
| F1 | product shell, `GET /v1/capabilities`, frontend gates | — | — |
| S1 | distributed runs / queues | — | — |
| S2 | auth, multi-tenancy | — | — |
| S10 | human-in-the-loop | — | — |
| S3/S4 | plugin strategies, MCP tools | — | — |
| S6 | workflow engine + canvas | — | — |
| S12/S11 | richer memory, evaluation | — | — |
| S14/S13 | multi-agent, triggers | — | — |
| S7/S9 | OTel tracing, hardening | — | — |