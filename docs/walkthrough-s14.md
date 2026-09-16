# S14 manual walkthrough — sub-agents and handoffs (ADR 0018), live script

A hands-on reproduction of the S14 acceptance behavior against a live
stack. Format follows walkthrough-s12. Backend :8001, web :5173, dev DB
`jarvis` (migration 0012), mock agents for the deterministic binding/pin
steps, real `gemma4:31b` for the runs that must actually call tools
(the mock provider emits its default reply and never calls tools live —
scripted turns exist only in-process).

S14 in one sentence: **agents can delegate and hand off** — a
`sub__<name>` binding spawns a child run the parent tool-call blocks on
(no session, shared trace, inherited deadline), a `handoff__<name>`
binding transfers the transcript to a successor run and ends this one;
pins are SERVER-STAMPED at publish and cycles 422 (D51–D56).

The invariants the walkthrough exercises:

- **D51 — the pin is the server's.** The editor sends only
  `{agent_id}`; `agent_version_id` is stamped at save and re-stamped at
  every publish. A stale pin is a lint, never a failure.
- **D52/D53 — children are ordinary runs.** They appear in
  `GET /v1/executions`, are fetchable by id, share the parent's
  `trace_id`, carry `session_id: null` and `parent_run_id`, and the
  detail page links them both ways.
- **D55 — children are non-interactive.** An approval a child needs is
  auto-declined honestly; the run completes (never a pause).
- **D56 — a handoff ends the predecessor.** The final message names the
  successor; the depth is NOT laundered (successor spawns at the
  parent's depth).

## 0. Setup

The backend must be restarted to pick up migration 0012
(`parent_run_id` column + index) and the S14 tooling:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0012: agent_executions.parent_run_id
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

auth_mode is `required` in `.env` — reuse your session identity (or
provision one, walkthrough-s6 §0 pattern), then:

```bash
export JARVIS_KEY="jarvis_sk_…"       # your existing api key
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity — the capabilities report the two new derived facts:

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities | python3 -c '
import json,sys
s = json.load(sys.stdin)["sections"]
print("agents.detail.sub_agents  →", s["agents"]["detail"]["sub_agents"])
print("executions.detail.sub_agents →", s["executions"]["detail"]["sub_agents"])'
# → True / True
```

## 1. The workers — two mock agents for the family to target

Children need a pinned published version, not a callable brain (a mock
child's runs are cheap and deterministic):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "s14-worker",
  "description": "S14 demo child",
  "model": {"provider": "mock", "model": "mock-1"},
  "strategy": {"type": "function_calling"}
}' | python3 -c 'import json,sys; b=json.load(sys.stdin); print(b["definition"]["id"], b["versions"][-1]["id"])'
export WORKER_ID="<id>"; export WORKER_VER="<version-id>"   # read both off the line above
```

NOTE (found live): `versions` is a SIBLING of `definition` in the
`AgentDetail` response, not nested inside it — parse at the top level
(`b["versions"]`), never `body["definition"]["versions"]`. Also paste
ids LITERALLY into later commands; the shell-interpolation forms
(`'"$VAR'"`) break interactive zsh pastes (dquote>/quote> traps), and
`<placeholder>` tokens sent over the wire un-replaced 422.

(One worker suffices for §2–§5; create `s14-worker-2` the same way if
you want the two-children receipt of §2 to show sibling rows.)

## 2. The supervisor — bind and delegate (gemma, real function calling)

The supervisor carries a `sub__s14-worker` binding; the config carries
ONLY `agent_id` — this is the client-visible half of D51. Paste the
worker id from §1 into the binding config literally:

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "s14-supervisor",
  "description": "S14 delegation demo",
  "model": {"provider": "openai_compatible", "model": "gemma4:31b",
            "base_url": "https://ollama.com/v1",
            "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}},
  "strategy": {"type": "function_calling"},
  "tools": [{"name": "sub__s14-worker", "config": {"agent_id": "PASTE-WORKER-ID"}}]
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
export SUPERVISOR="<supervisor-id-from-above>"
```

Receipt 1 — the pin was stamped server-side. Fetch the detail and check
the binding config carries BOTH ids (the client sent only the first):

```bash
curl -s -H "$AUTH" localhost:8001/v1/agents | python3 -c '
import json,sys
for a in json.load(sys.stdin)["items"]:
    if a["name"] == "s14-supervisor":
        for t in a["tools"]:
            if t["name"].startswith("sub__"):
                print(t["name"], t["config"])'
# → {"agent_id": "<WORKER_ID>", "agent_version_id": "<WORKER_VER>"}
```

Run it and let the model delegate ("use the sub__s14-worker tool"):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$SUPERVISOR/run \
  -d '{"input": "Delegate this task to s14-worker: summarize the S14 design in one sentence."}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["status"], r["run_id"])'
export PARENT_RUN="<run_id>"
```

Receipt 2 — the child is an ordinary run, findable through the parent:

```bash
curl -s -H "$AUTH" "localhost:8001/v1/executions?parent_run_id=$PARENT_RUN" \
  | python3 -c '
import json,sys
items = json.load(sys.stdin)["items"]
for r in items: print(r["run_id"], r["status"], r["agent_id"])'
```

```bash
curl -s -H "$AUTH" "localhost:8001/v1/executions/$PARENT_RUN" | python3 -c '
import json,sys
d = json.load(sys.stdin)
print("parent trace:", d["run"]["trace_id"])
for c in d["children"]:
    print("child:", c["run_id"], c["status"],
          "session:", c["session_id"], "trace:", c["trace_id"],
          "parent:", c["parent_run_id"])'
```

The invariants to eyeball: the child's `trace_id` EQUALS the parent's,
`session_id` is null, `parent_run_id` points at the parent. The child's
detail page (web or API) shows the same transcript machinery as any run
— nothing about it is special except the metadata (`sub_agent.depth`).

Receipt 3 — the parent's tool_executions row for `sub__s14-worker`
carries the child's usage in its metadata (D54):

```bash
curl -s -H "$AUTH" "localhost:8001/v1/executions/$PARENT_RUN" | python3 -c '
import json,sys
d = json.load(sys.stdin)
for t in d["tool_executions"]:
    if t["tool_name"].startswith("sub__"):
        print(t["tool_name"], "is_error:", t["is_error"])
        print("metadata:", t.get("metadata"))'
# → metadata.usage.input_tokens > 0 — the child's tokens folded into the parent's view
```

## 3. The handoff — transcript transfer, predecessor ends

A second supervisor with a `handoff__s14-worker` binding (paste the
worker id literally):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" localhost:8001/v1/agents -d '{
  "name": "s14-relay",
  "description": "S14 handoff demo",
  "model": {"provider": "openai_compatible", "model": "gemma4:31b",
            "base_url": "https://ollama.com/v1",
            "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}},
  "strategy": {"type": "function_calling"},
  "tools": [{"name": "handoff__s14-worker", "config": {"agent_id": "PASTE-WORKER-ID"}}]
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["definition"]["id"])'
export RELAY="<relay-id-from-above>"
```

Run it in a session and instruct the handoff ("use the handoff tool to
pass this to s14-worker, note: ..."):

```bash
curl -s -H "$AUTH" -H "Content-Type: application/json" \
  localhost:8001/v1/agents/$RELAY/run \
  -d '{"input": "Hand this off to s14-worker", "session_id": "s14-relay"}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["status"], r["run_id"], "|", r.get("final_message"))'
```

The predecessor's final message names the successor:
**"Handed off to s14-worker (run <id>)"** — an ordinary `run.completed`
(ADR 0003 untouched; D56). Fetch the SUCCESSOR run id out of that
message, then:

```bash
curl -s -H "$AUTH" "localhost:8001/v1/executions/$SUCCESSOR" | python3 -c '
import json,sys
d = json.load(sys.stdin)
print("session:", d["run"]["session_id"], "trace:", d["run"]["trace_id"])
print("input:", d["run"]["input"][:300])
print("handoff metadata:", d["run"]["metadata"].get("handoff"))'
```

The invariants: the successor shares the predecessor's session AND
trace; its input is the rendered transcript (`[user] …` lines) plus
`[handoff note] …` when the model passed one; the handoff metadata
names the note. The depth is unchanged (`sub_agent.depth` equals the
predecessor's) — a handoff cannot launder the cap.

## 4. Web tour — the editor picker and the children view

Open http://localhost:5173:

- **Agents → any agent → Tools**: with `sub_agents` on, the panel has
  two selects — *Delegate to sub-agent* and *Hand off to agent* —
  listing the tenant's OTHER agents (the agent being edited is absent;
  self-binding is a server 422). Pick one: a `sub__<name>` (or
  `handoff__<name>`) binding row appears with a JSON config carrying
  only `agent_id`. Save → detail page shows the pin the server stamped.
- **Executions → the §2 parent run**: a "Sub-agent runs" section lists
  each child as a labeled link (`delegation · s14-worker` / `handoff ·
  s14-worker`), and a child's own detail page shows a *Parent run*
  link back. Trace both directions.

## 5. Honesty paths — cap, auto-decline, stale pins, cycles

**Depth cap (D55).** Live-staged as a 3-hop chain: supervisor → mid1 →
mid2, each gemma with a `sub__` binding to the next, mid2 targeting the
mock worker. mid2 sits at depth 2, so its `sub__` call attempts depth 3
and is refused: the tool result is an honest is_error — `SubAgentError:
sub-agent depth cap (2) reached — delegation to depth 3 refused` — and
mid2's run still SUCCEEDS (the model narrates the refusal). No run row
exists for the refused delegation. (A 2-hop chain stays under the cap:
the refusal only fires at the FOURTH level.)

**Auto-decline (D55).** Bind an approval-gated tool (S4 pattern —
`requires_approval: true` in the binding config) to a gemma child
agent, then delegate into it with a prompt that FORCES the tool: ask
for `12839 * 7173` — "2+2" fails as a probe because gemma answers it
without the tool (found live). The child's approval request is answered
WITHOUT a human — the decline copy goes back as the tool's transcript
result — and NOTHING ever pauses (no `awaiting_input` row anywhere in
the chain; the child run succeeds).

RECEIPT LOCATION (found live): an auto-declined call leaves **no
tool-execution row** — the evidence lives in the transcript only.
Read it from the child's llm-trace: iteration 0 shows the model's
`calculator` tool_call, the next iteration's request messages carry the
decline tool result. The exact copy: *"No human approver is available
in this run, so this call was not executed. Continue without it and
answer from what you know."* Expect the S10 decline→re-ask loop: gemma
re-called the declined tool ~3× (the transcript accumulates one tool
message per decline) before giving up and answering narratively.

**Stale-pin lint + cycle 422 (D51).**

```bash
# republish the worker (PATCH with a trivial body change) → its version bumps
# the supervisor's pin is now stale — a LINT on detail, never a failure:
curl -s -H "$AUTH" localhost:8001/v1/agents/$SUPERVISOR | python3 -c '
import json,sys; print(json.load(sys.stdin)["definition"]["lints"])'
# → "tool 'sub__s14-worker' pins a stale version of agent '…' — republish to pin the latest"

# cycle: PATCH supervisor → sub__s14-worker is fine, but PATCH s14-worker
# to sub__ the supervisor (A→B→A) must 422:
# → "sub-agent cycle through <id>"
```

## 6. What is unit-covered, not live-scripted

- **Cancel fan-out** (parent cancelled mid-child ends both) and the
  three bounded child endings (cancel observed / wait-ended /
  parent-deadline) — timing-fragile to stage live; pinned by the
  integration suite (`test_cancel_parent_mid_child_ends_both`) and the
  provider unit tests.
- **SSE keep-alive pings** during a quiet child wait (`PING_AFTER_SECONDS`,
  default 15s — watch a §2 delegation's stream for `: keep-alive`
  frames if you want the live flavor; the integration test monkeypatches
  the interval to 0.05s).
- **Tenant scoping** of children (a foreign tenant's list is empty and
  the child detail 404s) — `test_children_are_tenant_scoped`.
- **Handoff without a note** (metadata carries `note: null`, no
  `[handoff note]` block) — unit-covered.

## 7. Teardown / receipts

```bash
psql "postgresql://jarvis:jarvis@localhost/jarvis" \
  -c "SELECT id, status, parent_run_id, session_id IS NULL AS childless_session
      FROM agent_executions WHERE trace_id IN (SELECT trace_id FROM agent_executions
      WHERE id = '$PARENT_RUN') ORDER BY started_at"
# the whole family tree: parent + children (children with NULL session).
# NOTE: the run id column is `id`, NOT `run_id` (agent_executions' PK, ADR 0003).
curl -s -H "$AUTH" localhost:8001/v1/agents | python3 -m json.tool | grep '"name"'
# s14-worker / s14-supervisor / relay remain for the session; delete if unwanted
```

## 8. Session notes — findings from the live run

Session ran 2026-09-16, all sections green. Demo agents created live:
s14-worker (mock), s14-supervisor, s14-relay, plus the depth-cap chain
(s14-mid1, s14-mid2) and s14-gated (approval-gated calculator child).
Every receipt above was verified live; the §5 honesty paths were all
exercised with real gemma4:31b runs.

**Bonus demo — deep research as a sub-agent tree (web UI only):** four
gemmad agents built from the editor — `dr2-lead` (delegates to
`dr2-reader` with a tavily MCP search tool, requires_approval OFF, and
`dr2-writer`; the writer delegates to `dr2-critic`). One user prompt
produced a four-run tree (lead → reader + writer → critic: depth-2
grandchild, all within the cap), one shared trace, sequential blocking
delegations (09:58:04 → :06 → :20 → :22), and a sourced 3-paragraph
brief. Prompting lessons: delegation instructions must be
self-contained (children start cold — sessionless, memory-less); an
instruction like "has been internally reviewed" lets the writer SKIP
its critic delegation (harden to "you MUST delegate to dr-critic");
small probes like "2+2" don't force tool calls.

**Doc fixes folded from the session:** `AgentDetail.versions` parses at
the TOP level (sibling of `definition` — §1 fixed); interpolation
commands rewritten as literal-id pastes (§2/§3); the auto-decline probe
and receipt location (§5); §7's query uses `id`, not `run_id`.

**Backlog candidates:** a whole-tree roll-up on the root run's detail
page (children render one hop deep by design — the critic is only
visible on the writer's page, not the lead's, and the lead's run
console shows only the lead's own tool calls); a sub-agent factory
(agent creates an agent at runtime) — recorded in roadmap as
deliberately out (publish-frozen targets: replay fidelity, cycle
safety, tenancy), ADR territory if ever revisited. The `sub__` binding
zoo pattern (pre-created role workers the supervisor chooses among)
covers most "dynamic team" needs today.