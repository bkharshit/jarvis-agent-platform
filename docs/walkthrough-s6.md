# S6 manual walkthrough — the workflow engine (ADR 0015), live script

A hands-on reproduction of the S6 acceptance tests against a live stack.
Format follows walkthrough-s10/s4. Backend :8001, web :5173, dev DB
`jarvis` (migration 0008), mock agents for deterministic steps, real
`gemma4:31b` for the human-readable steps.

The engine in one sentence (D41): **a workflow run IS an agent execution**
— `agent_id` holds the workflow id, `metadata.kind == "workflow"`, the
event envelope is the frozen ADR 0003 shape with `node.started` /
`node.completed` carrying an optional `node_id` (D43). Three things
compose from stages you already know: the runtime's segment machinery,
the S10 pause/resume chain, and the D37/D38 MCP tool resolution.

Three decisions the walkthrough exercises:

- **D42 — pins happen at publish.** An agent node carries
  `agent_version_id` stamped by the create/update routes (the latest
  published version at that moment). A stale pin is a *lint warning*
  shown in the detail's `lints` — the run still executes the pinned
  snapshot. No pin target at all is a **422** at save (never a runtime
  surprise).
- **D43 — no `node.failed`.** Inner failures surface as the run's
  terminal `run.failed` with `node '<id>':` in the message. Terminals
  are run-level, never node-scoped.
- **D44 — the walk is sequential with a node-execution cap** (default
  24, `max_node_executions` 1–128). Exceeding it is a persisted
  `run.failed` naming the node, not an exception.

## 0. Setup

The backend must carry the S6 routes (commit 7+) — if it predates them,
`GET /v1/workflows` 404s. Restart it, then provision a walkthrough user
(auth_mode is `required` in `.env`):

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migration 0008: workflows + versions
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz

# one-time session identity (owner on the default tenant — it sees the
# NULL-tenant agents created in anonymous mode)
uv run jarvis user create default s6-owner@jarvis.test --role owner --password s6s3cret
uv run jarvis api-key create s6-owner@jarvis.test --name s6-walkthrough
# → copy the plaintext once:
export JARVIS_KEY="jarvis_sk_…"       # plaintext, printed once
export AUTH="Authorization: Bearer $JARVIS_KEY"
```

Sanity: the capability is a derived fact (same S4 lesson — it reads the
`workflows` table, which migration 0008 creates):

```bash
curl -s -H "$AUTH" localhost:8001/v1/capabilities | python3 -m json.tool | grep -A3 workflows
#  "workflows": { "enabled": true, "summary": "DAG runs reusing the same event model" }
```

## 1. Create a workflow — pins stamped at create (D42)

```bash
A1=$(curl -s -H "$AUTH" localhost:8001/v1/agents | python3 -c \
  "import json,sys; print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='approval-agent'][0])")
# pick any two mock agents; approval-agent (mock) is fine for determinism

curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows -H 'Content-Type: application/json' -d '{
  "name": "s6-chain",
  "description": "two agent nodes in sequence",
  "nodes": [
    {"id": "a", "type": "agent", "config": {"agent_id": "'"$A1"'", "input_template": "{{input}}"}},
    {"id": "b", "type": "agent", "config": {"agent_id": "'"$A1"'", "input_template": "{{node.a}}"}}
  ],
  "edges": [{"from_node": "a", "to_node": "b"}],
  "start_node_id": "a"
}' | python3 -m json.tool
```

What to look at in the response:

- `definition.nodes[*].config.agent_version_id` is **non-null** — the
  create route pinned each agent node to its agent's latest published
  version before persisting (you sent none).
- `versions: [{version: 1, ...}]` — create publishes v1 immediately.
- `lints: []` — pins are fresh, nothing to warn about.

Templates: `{{input}}` is the run input; `{{node.<id>}}` is an upstream
node's output (the output IS the value — **not** `{{node.<id>.output}}`:
that walks three hops and the third hop into a string misses, passing
the placeholder through untouched — live-found 2026-09-14; the mock
provider masked it in §3 because it ignores its input, gemma exposed it
in §6). Plain substitution only — no Jinja (ADR 0015 §2).

## 2. Graph validation — 422s, never a broken definition

```bash
GHOST=$(uuidgen)   # an agent id that does not exist

# ghost agent → 422 (D42: "no published version = 422 at save")
curl -s -w '\n%{http_code}\n' -H "$AUTH" -X POST localhost:8001/v1/workflows \
  -H 'Content-Type: application/json' -d '{
    "name": "s6-ghost",
    "nodes": [{"id":"a","type":"agent","config":{"agent_id":"'"$GHOST"'","input_template":"{{input}}"}}],
    "start_node_id": "a"}' | tail -2

# cycle → 422
curl -s -w '\n%{http_code}\n' -H "$AUTH" -X POST localhost:8001/v1/workflows \
  -H 'Content-Type: application/json' -d '{
    "name": "s6-cycle",
    "nodes": [{"id":"a","type":"agent","config":{"agent_id":"'"$A1"'","input_template":"{{input}}"}},
              {"id":"b","type":"agent","config":{"agent_id":"'"$A1"'","input_template":"{{input}}"}}],
    "edges": [{"from_node":"a","to_node":"b"},{"from_node":"b","to_node":"a"}],
    "start_node_id": "a"}' | tail -2

# unknown start node → 422; condition referencing a missing node → 422
```

Each rejection is a clean 422 `validation` envelope with the pydantic
loc/msg — and **no row was created** (the name is free to reuse).

## 3. Run the walk (blocking) — events carry node_id

```bash
RUN=$(curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows/$(WF_ID)/run \
  -H 'Content-Type: application/json' -d '{"input": "walk me"}')
echo "$RUN" | python3 -m json.tool
# status "succeeded"; metadata.kind == "workflow"; agent_id == workflow id (D41)

curl -s -H "$AUTH" localhost:8001/v1/executions/$RUN_ID/events \
  | python3 -c "import json,sys; [print(e['sequence'], e['type'], e.get('node_id')) for e in json.load(sys.stdin)['items']]"
```

Expected shape for the two-node walk: `run.started` →
`node.started(node_id=a)` → that node's iteration/message events →
`node.completed(node_id=a, output=...)` → `node.started(node_id=b)` → …
→ `node.completed(node_id=b)` → `run.completed`. Node b's template
`{{node.a}}` resolved into b's input (verify substitution with the REAL
model in §6 — the mock provider ignores its input, so identical outputs
here do not prove resolution). The mock agents reply
"This is a mock response." — identical text, deterministic walk.

The executions list now names the workflow (`names` map, commit 7):

```bash
curl -s -H "$AUTH" 'localhost:8001/v1/executions?limit=5' \
  | python3 -m json.tool | grep -A3 '"names"'
# { "<workflow-uuid>": "s6-chain" }
```

## 4. Stream — SSE frames with node events (D43 on the wire)

```bash
curl -s -N -H "$AUTH" -X POST localhost:8001/v1/workflows/$(WF_ID)/stream \
  -H 'Content-Type: application/json' -d '{"input": "stream me"}' | head -30
```

`event:` lines match the agent stream's frame format exactly — only the
path differs. `node.started`/`node.completed` frames carry `node_id`.
The run console on the web renders this as the node groups (§8).

## 5. Tool node + condition node — the non-agent vocabulary

`current_time` takes no arguments (clean, deterministic); the condition
routes on the **most recent upstream output** — in the `a → t → c`
chain that is the *tool* node's output — with a closed operator set
(contains / equals / regex / not_empty), first match wins, else
required:

```bash
curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows -H 'Content-Type: application/json' -d '{
  "name": "s6-branch",
  "nodes": [
    {"id": "a",  "type": "agent",     "config": {"agent_id": "'"$A1"'", "input_template": "{{input}}"}},
    {"id": "t",  "type": "tool",      "config": {"binding": {"name": "current_time", "config": {}}, "arguments": {}}},
    {"id": "c",  "type": "condition", "config": {"routes": [{"when": {"operator": "contains", "value": "T"}, "to_node": "yes"}],
                                              "else_node": "no"}},
    {"id": "yes","type": "agent",     "config": {"agent_id": "'"$A1"'", "input_template": "affirmative branch, time was {{node.t}}"}},
    {"id": "no", "type": "agent",     "config": {"agent_id": "'"$A1"'", "input_template": "else branch taken"}}
  ],
  "edges": [{"from_node":"a","to_node":"t"},{"from_node":"t","to_node":"c"},
            {"from_node":"c","to_node":"yes"},{"from_node":"c","to_node":"no"}],
  "start_node_id": "a"
}' | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['definition']['id'], d['lints'])"
```

Run it: the condition evaluates `node t`'s output (an ISO timestamp —
contains "T") → the walk takes the `yes` node; `no` never executes
(check the events — no `node.started(node_id=no)`, and the condition's
own `node.completed` output names the node it routed to). Flip `value`
to "impossible" and re-PATCH → next run takes the else branch. Tool-node
templating: a string argument like `"{{node.a}}"` substitutes like an
input template.

## 6. HITL inside a node — S10 composes, not deferred

A workflow whose agent node runs an approval-gated tool pauses
*inside* the node:

```bash
# approval-gemma3 (real model, calculator with requires_approval)
curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows -H 'Content-Type: application/json' -d '{
  "name": "s6-hitl",
  "nodes": [
    {"id": "ask", "type": "agent",
     "config": {"agent_id": "'"$GEMMA_APPROVAL"'", "input_template": "What is 17 * 23? Use the calculator."}},
    {"id": "wrap","type": "agent",
     "config": {"agent_id": "'"$GEMMA_APPROVAL"'", "input_template": "Summarize in one sentence: {{node.ask}}"}}
  ],
  "edges": [{"from_node":"ask","to_node":"wrap"}],
  "start_node_id": "ask"
}'
```

Run via `/stream` and watch:

- the run **pauses**: `run.awaiting_input` arrives while
  `node.started(node_id=ask)` is still open — no `node.completed` yet
  (D43: the node is not done, the run is not terminal).
- Resume exactly like S10 —
  `POST /v1/executions/$RUN_ID/resume` with
  `{"decisions": {"<tool_call_id>": true}}` (a tool_call_id → approve/
  decline MAP — the dict shape is load-bearing; a list 422s). The
  resumed segment continues the SAME walk: `node.completed(node_id=ask)`
  fires after the tool runs, then `node.started(node_id=wrap)`, then the
  terminal.

## 7. Node cap (D44) — the walk stops as a run failure

```bash
# three-node chain, cap 2
curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows -H 'Content-Type: application/json' -d '{
  "name": "s6-cap",
  "nodes": [
    {"id":"a","type":"agent","config":{"agent_id":"'"$A1"'","input_template":"{{input}}"}},
    {"id":"b","type":"agent","config":{"agent_id":"'"$A1"'","input_template":"{{input}}"}},
    {"id":"c","type":"agent","config":{"agent_id":"'"$A1"'","input_template":"{{input}}"}}
  ],
  "edges": [{"from_node":"a","to_node":"b"},{"from_node":"b","to_node":"c"}],
  "start_node_id": "a", "max_node_executions": 2
}' # save the id, then:

curl -s -H "$AUTH" -X POST localhost:8001/v1/workflows/$(CAP_ID)/run \
  -H 'Content-Type: application/json' -d '{"input":"go"}' | python3 -m json.tool
# terminal run.failed — "workflow exceeded max_node_executions=2"
# (ADR 0015: the error names the CAP, not the node — which node tripped
# it is visible in the event trail: the last node.started before the
# terminal is 'b', the one that would have been next is never started)
```

## 8. The web UI — Workflows section

`cd web && npm run dev` → http://localhost:5173/workflows (log in as
`s6-owner@jarvis.test` / `s6s3cret` — the section gates on
capabilities, which flipped on in commit 7).

1. **List** shows the workflows created above with node counts and
   Run links; the empty state shows before the first create.
2. **Canvas editor** — `+ agent` adds a node; the config panel's agent
   picker lists live agents and shows the D42 pin note; `+ tool` lists
   builtin tools from capabilities; `+ condition` configures routes
   with target pickers. Save strips UI-only (`_`-prefixed) keys — the
   server payload is `extra="forbid"`.
3. **Concurrent-edit guard** — leave the editor open in two tabs,
   save in one, then save in the other: the second save refuses with
   "changed on the server" (server-hash compare, no silent clobber).
4. **Run console** — Run from the workflow page streams into the same
   console as agents; each node's inner events render **inside its
   node group card**; a pause shows the shared PauseCard and the
   resumed segment's events land back in the same group until its
   `node.completed` closes it.
5. **Executions** — the list shows the workflow's name (resolved via
   the `names` map); the detail page labels it "Workflow" and groups
   the timeline by node.
6. **Delete** — a workflow with executions refuses (409, toast);
   a fresh one deletes (204, row gone from the list).

## 9. Stale pin — a lint, never a failure (D42)

Edit `approval-agent` (any PATCH publishes a new version), then GET the
`s6-chain` detail: `lints` now carries a stale-pin warning naming the
node. **The run still works** — it executes the pinned snapshot (D1
replay fidelity, one level up). Re-PATCH the workflow to re-pin.

```bash
curl -s -H "$AUTH" localhost:8001/v1/workflows/$(WF_ID) | python3 -c \
  "import json,sys; print(json.load(sys.stdin)['lints'])"
```

## 10. Teardown / receipts

The receipts live in the dev DB: `workflows` + `workflow_versions`
rows (immutable append-only, D1 one level up) and `agent_executions`
rows with `metadata.kind = "workflow"`. Leave the demo workflows in
place for the UI walkthrough, then clean up what you don't want to
keep with DELETE (fresh ones only — executed workflows refuse).

## 11. Manual-session notes (2026-09-14) — live-found fixes and extras

Harshit's manual session went beyond the script; three findings worth
keeping with the walkthrough:

1. **Hyphenated node ids were unreferenceable (real backend bug, fixed
   `d535d7c`).** The canvas auto-generates `agent-1`, `agent-2`, … and
   `{{node.agent-2}}` NEVER matched `_TEMPLATE_PATTERN` (`[\w.]+` admits
   dots but not hyphens) — the placeholder passed through to the model
   literally and the agent answered the placeholder as if it were the
   task ("Please provide a number."). Every test and fixture used bare
   ids, so nothing caught it until a real UI run. The pattern is now
   `[\w.-]+`, with a regression test pinning the invariant: the template
   pattern must admit exactly what `_NODE_ID_PATTERN` admits — widen or
   narrow them together.

2. **`.output` is not a hop** (noted in §1): the output IS the value.
   `{{node.<id>.output}}` walks three dotted hops and the third hop into
   a string misses — pass-through. Harshit hit this with gemma
   (the mock provider ignores its input and masked it).

3. **Canvas ergonomics shipped from his asks (`1e8415d`)**: node ids are
   editable in the config panel (validates against the node-id pattern;
   a rename rewrites `{{node.<id>}}` refs in every node config — dotted
   hops, prefix-safe — plus edges, edge ids, `start_node_id`, and
   condition `to_node`/`else_node` targets), and the agent panel carries
   an inline **`+ new agent`** quick-create card riding the same draft
   payload rules as the Agents editor, auto-selecting the created agent
   on the node.

The session's capstone: a **deep-research workflow** mapped from an
existing LangChain pipeline — search (tavily MCP tool node) → reader →
writer → critic, four agent nodes with `{{input}}` / `{{node.search}}` /
`{{node.reader}}` / `{{node.writer}}` templates. It ran end-to-end on
the live stack. Practical notes if you build one: set the run timeout
generously (the platform default is 120 s; a four-stage research chain
wants ~600), remember the walk is acyclic — a revision loop needs to be
modeled differently in v1 — and nodes are memory-less (state flows only
through templates).

One operational incident during the session, unrelated to the engine:
a `set -a; source .env` in a restart flow clobbered the process-env
credentials master key (`.env` carried a var-name indirection, not the
key) and three stored credentials became undecryptable — rotation was
the only path. The full diagnosis and the `.env` rule live in
CLAUDE.md's gotchas and `.env.example`.