# S3 manual walkthrough — plugin strategies (D35/D36), verified live

A hands-on reproduction of the S3 acceptance tests against a live stack,
with the "why" behind each step. Verified 2026-09-07 against dev DB
`jarvis`, backend on :8001, real model `gemma4:31b` via Ollama Cloud (BYO
key in `.env`, referenced by *name* — never pasted into config, D29/D30),
and the fixture dist `jarvis-strategy-fixtures` (editable path dep from
the `dev` extra) supplying two sample plugins: `plan_execute` and
`raise_plugin`.

Two rules to keep in mind while reading:

- **D35 — plugins load at boot, never at run time.** Entry points in the
  `jarvis.strategies` group load only if the distribution's name is on
  `Settings.strategy_plugin_allowlist` (env
  `JARVIS_STRATEGY_PLUGIN_ALLOWLIST`, CSV, empty default). There is no hot
  load: pip/uv install, edit the allow-list, restart. Import failures are
  recorded as `failed`, allow-listed-but-absent names as `missing` —
  neither boots-fatal.
- **D36 — every boundary refuses, and refusal is a terminal event.** The
  strategy `type` is a free string (so plugins can extend the set), but
  the create/update boundary validates it (API 422, CLI exit 1) against
  the registry, and the runtime resolve boundary persists a terminal
  `run.failed` with `error_kind="strategy"`. A plugin whose `step()`
  raises gets the same treatment — a run never escapes the loop (D5).

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv sync --extra dev                   # installs jarvis-strategy-fixtures (editable path dep)
export JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
```

**Export the allow-list — do not inline it on the serve command only.**
The CLI is a separate process that loads its own registry (and its own
allow-list); with an inline env var the serve process knows `plan_execute`
but `jarvis agent create --file` still rejects it with
`unknown strategy type 'plan_execute' (known: function_calling, react)`.
Exporting covers serve, CLI, and any `jarvis worker` started from the same
shell. (Also documented as the allow-list-drift caveat in
`docs/plugins/strategy-plugins.md`.)

The plan-execute agent pins its model *and* its credential — env vars only
fill provider-less defaults (D28 gotcha):

```yaml
# /tmp/plan-execute-agent.yaml
name: plan-execute-agent
description: Runs the plan_execute plugin strategy from jarvis-strategy-fixtures.
model:
  provider: openai_compatible
  model: gemma4:31b
  base_url: https://ollama.com/v1        # pin the endpoint (D28 gotcha)
  credential_ref:
    type: env
    env_var: OLLAMA_API_KEY
system_prompt: >-
  You are a careful planner. First write a numbered plan. Then execute each
  step in order, one at a time. When EVERY step is complete, START YOUR
  REPLY with the exact line DONE: followed by a one-line summary.
strategy:
  type: plan_execute                     # loaded from the plugin distribution
  params:
    plan_marker: "PLAN:"                 # defaults, shown for the record
    done_marker: "DONE:"
tools:
  - name: current_time
memory:
  enabled: false
  max_messages: 20
```

```bash
uv run jarvis agent create --file /tmp/plan-execute-agent.yaml
PLAN_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='plan-execute-agent'][0])")
echo "agent: $PLAN_AGENT"
```

(If this walkthrough was already run once, the create line reports the
existing name and the id fetch above still works — runs pin the current
published version, so a patched prompt would be a *new* version, never a
rewrite, D1.)

## 1. The capabilities fact the UI flips on

```bash
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A14 '"plugins"'
# → "plugins": { "enabled": true, "detail": { "strategies": [
#      { "name": "function_calling", "origin": "builtin", ... },
#      { "name": "plan_execute", "origin": "plugin",
#        "distribution": "jarvis-strategy-fixtures", "version": "0.1.0" },
#      { "name": "react", "origin": "builtin", ... } ],
#      "failed": [], "missing": [], "allowlist": ["plan_execute"] } }
```

**Proves** the UI-enablement gate (decision 1.6): the Plugins section and
the agent editor's strategy picker read this payload — a plugin appears in
the product with zero web-specific code for it. `origin`, `distribution`
and `version` are the honesty fields: they say *where a strategy came
from*, and the web Plugins page renders them as badges.

## 2. A plugin strategy runs through the runtime unchanged

```bash
curl -s -X POST localhost:8001/v1/agents/$PLAN_AGENT/run \
  -H 'content-type: application/json' \
  -d '{"input":"What is 6 hours in minutes?"}' \
  --max-time 420 -o /tmp/s3-plan-run.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s3-plan-run.json'))
print(d['status'], '| run:', d['run_id'], '| iterations:', d['iterations'])
print('final:', (d.get('final_message') or '').replace('\n', ' ⏎ '))
EOF
# → succeeded | iterations: 4
#   final: 3. 6 hours is equal to 360 minutes. ⏎ DONE: 6 hours is 360 minutes.
```

The transcript is the textbook plan-and-execute shape — every entry below
is one `plan_execute.step()` returning a `ToolCallsStep` with empty
`tool_calls` (a **think step**: the orchestrator persists the assistant
message and continues the loop) until the done marker appears:

| turn | role | text | phase |
|---|---|---|---|
| 1 | assistant | `PLAN:` + numbered steps | plan (think step) |
| 2 | assistant | executes plan step 1 | execute (think step) |
| 3 | assistant | executes plan step 2 | execute (think step) |
| 4 | assistant | step 3 + `DONE: ...` | execute → `FinishStep` |

```bash
RUN_ID=$(python3 -c "import json;print(json.load(open('/tmp/s3-plan-run.json'))['run_id'])")
psql "$JARVIS_DATABASE_URL" -At -c "
select sequence, role, left(regexp_replace(content::text, E'[\\n\\r]+', ' ⏎ ', 'g'), 110)
from messages where execution_id='$RUN_ID' order by sequence;"
```

**Proves** the think-step mechanism (the S3 plan's core claim): a plugin
drives multi-phase behavior purely through transcript markers — stateless
by construction, no runtime changes, no new event types. The plugin emits
its own `model.invocation.*` events through the same sink the core
strategies use, so the executions detail page shows every model call.

## 3. A plugin that raises cannot crash a run (D36 / D5)

Now allow-list the second fixture plugin. **D35 means a restart** — there
is no hot load:

```bash
export JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute,raise_plugin
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
JARVIS_STRATEGY_PLUGIN_ALLOWLIST=$JARVIS_STRATEGY_PLUGIN_ALLOWLIST uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

```yaml
# /tmp/raise-plugin-agent.yaml — step() always raises RuntimeError
name: raise-plugin-agent
description: Runs the raise_plugin fixture strategy — its step() always raises.
model:
  provider: openai_compatible
  model: gemma4:31b
  base_url: https://ollama.com/v1
  credential_ref:
    type: env
    env_var: OLLAMA_API_KEY
system_prompt: You are terse.
strategy:
  type: raise_plugin
tools: []
memory:
  enabled: false
  max_messages: 20
```

```bash
uv run jarvis agent create --file /tmp/raise-plugin-agent.yaml
RAISE_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='raise-plugin-agent'][0])")
curl -s -X POST localhost:8001/v1/agents/$RAISE_AGENT/run \
  -H 'content-type: application/json' -d '{"input":"hello"}' \
  --max-time 120 -o /tmp/s3-raise-run.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s3-raise-run.json'))
print(d['status'], '| error_kind:', d.get('error_kind'))
print('error:', d.get('error'))
EOF
# → failed | error_kind: strategy
#   error: strategy 'raise_plugin' raised RuntimeError: raise_plugin always raises (fixture)
```

Note the HTTP status: **200**. The run did not fail *because the API
raised* — it failed because the runtime caught the exception at the step
call site, persisted the terminal `run.failed` with
`error_kind="strategy"`, and returned normally. The worker acked the
message; there is no retry loop, no stack trace past the runtime.

The same contract holds at the resolve boundary, which §5 exercises.

## 4. Empty allow-list: the boundary refuses, and the snapshot outlives the plugin

```bash
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
env -u JARVIS_STRATEGY_PLUGIN_ALLOWLIST uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3
# plugins vanish from capabilities — builtins only:
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A6 '"plugins"'
# creating a plugin-strategy agent is refused (422, with the known list):
curl -s -X POST localhost:8001/v1/agents -H 'content-type: application/json' \
  -d '{"name":"blocked-plugin-agent","description":"should be refused",
       "model":{"provider":"mock","model":"mock-agent"},
       "system_prompt":"hi","strategy":{"type":"plan_execute","params":{}},
       "tools":[],"memory":{"enabled":false,"max_messages":20}}' \
  -w "\nHTTP %{http_code}\n" | tail -4
```

**Proves** the create boundary (D36): typo protection lives with the
registry, not the type — `StrategyConfig.type` is a free string, so the
refusal is runtime-derived (`unknown strategy type 'plan_execute' (known:
function_calling, react)`), and a new plugin widens the accepted set
without a code change.

But the existing agent still runs — its published version 3 snapshot
(ADR 0002, immutable append-only, D1) pinned `strategy.type:
plan_execute`, and the run resolves the strategy from the *snapshot*:

```bash
curl -s -X POST localhost:8001/v1/agents/$PLAN_AGENT/run \
  -H 'content-type: application/json' -d '{"input":"What is 6 hours in minutes?"}' \
  --max-time 120 -o /tmp/s3-pinned-run.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s3-pinned-run.json'))
print(d['status'], '| error_kind:', d.get('error_kind'))
print('error:', d.get('error'))
EOF
# → failed | error_kind: strategy
#   error: "unknown strategy: 'plan_execute' (known: ['function_calling', 'react'])"
```

**Proves** the resolve boundary: the run is created (HTTP 200), then
persisted as terminal `run.failed` with `error_kind="strategy"` — the
agent's history stays honest about what happened to its old version. (In
a distributed deployment the same message would be claimed by a worker;
identical terminal state.)

## 5. Teardown and the web UI

```bash
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &    # standard env (empty allow-list)
```

Both walkthrough agents keep their executions, so `jarvis agent delete`
refuses them — that is the audit trail, not clutter. With the standard
(empty) allow-list back, open http://localhost:5173 and:

- **Plugins page** — the section is live (capability-gated, S3 flipped
  it): strategy listing with `builtin`/`plugin` origin badges, the
  distribution + version line for plugins, and the honest empty-allow-list
  panel ("Empty — no plugins load…") naming
  `JARVIS_STRATEGY_PLUGIN_ALLOWLIST` and the no-hot-load rule.
- **Agent editor** — the strategy dropdown offers exactly what
  capabilities reports. With the plugin allow-listed it offers
  `plan_execute`; without it, creating that agent 422s at the API — the
  picker never lies.
- **Executions** — the runs from §2–§4 are there: the succeeded
  plan-execute run replays its plan→execute→DONE transcript; the §3/§4
  failures render as red `run.failed` rows with
  `error_kind: strategy` and the exact refusal message.

## 6. Fixes from live testing

Two fixture bugs surfaced while running `plan_execute` against real
gemma4:31b (the mock-provider integration tests could not catch either,
because scripted replies carry the markers regardless of instructions).
Fixed in `1da2592`:

1. **The planner instruction was dead code.** The plan branch sent bare
   `messages` — `_PLAN_INSTRUCTION` existed but was never appended, so no
   real model ever emits `PLAN:`, the phase never flips, and the loop
   burns to `max_iterations`. The planner instruction now rides along,
   mirroring the executor branch.
2. **Done-marker matching was prefix-based; gemma appends, not prefixes.**
   `lstrip().startswith("DONE:")` never fired on "…DONE: 6 hours is 360
   minutes" — the run looped to `max_iterations` on a reply that was
   *already done*. Matching is now substring, like the plan phase: real
   models do not reliably obey marker-placement instructions.

The third finding is a walkthrough discipline, not code: **the CLI is its
own process.** An allow-list inlined on the serve command leaves the CLI's
registry without the plugin — export `JARVIS_STRATEGY_PLUGIN_ALLOWLIST`
for the whole session instead (§0). Same rule, and the same drift risk,
for distributed workers: `docs/plugins/strategy-plugins.md` carries the
caveat.

The fourth: **a plugin that calls `client.generate()` gets no live
transcript.** The core strategies stream — `function_calling` consumes
`client.stream()` and forwards each chunk as a `text.delta` event, which
is what the run console renders under each iteration. The fixture's plain
`generate()` produced a correct run with *empty* iterations in the UI
(markers only, text nowhere until the final message). The fixture now
mirrors the core discipline: stream when `client.capabilities.streaming`,
fall back to `generate()` otherwise. Lesson for plugin authors: **the run
console renders `text.delta` events — forward them if you want live text.**

## Appendix — copy-paste-safe shell (the S2 lesson)

The §2/§3 transcript queries use psql against dev Postgres. If
`JARVIS_DATABASE_URL` is not set in the shell, prefix it:

```bash
export JARVIS_DATABASE_URL="postgresql://jarvis:jarvis@localhost/jarvis"
```

And the full sequence, in one block (assumes §0 exports are in place):

```bash
export JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv sync --extra dev
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
# §1 — capabilities
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A14 '"plugins"'
# §2 — create + run
uv run jarvis agent create --file /tmp/plan-execute-agent.yaml
PLAN_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='plan-execute-agent'][0])")
curl -s -X POST localhost:8001/v1/agents/$PLAN_AGENT/run -H 'content-type: application/json' \
  -d '{"input":"What is 6 hours in minutes?"}' --max-time 420 -o /tmp/s3-plan-run.json -w "HTTP %{http_code}\n"
# §3 — raise_plugin (restart required: D35 has no hot load)
export JARVIS_STRATEGY_PLUGIN_ALLOWLIST=plan_execute,raise_plugin
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
JARVIS_STRATEGY_PLUGIN_ALLOWLIST=$JARVIS_STRATEGY_PLUGIN_ALLOWLIST uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3
uv run jarvis agent create --file /tmp/raise-plugin-agent.yaml
RAISE_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='raise-plugin-agent'][0])")
curl -s -X POST localhost:8001/v1/agents/$RAISE_AGENT/run -H 'content-type: application/json' \
  -d '{"input":"hello"}' --max-time 120 -o /tmp/s3-raise-run.json -w "HTTP %{http_code}\n"
# §4 — empty allow-list (restart, no env var)
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
env -u JARVIS_STRATEGY_PLUGIN_ALLOWLIST uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3
curl -s -X POST localhost:8001/v1/agents -H 'content-type: application/json' \
  -d '{"name":"blocked-plugin-agent","description":"should be refused",
       "model":{"provider":"mock","model":"mock-agent"},
       "system_prompt":"hi","strategy":{"type":"plan_execute","params":{}},
       "tools":[],"memory":{"enabled":false,"max_messages":20}}' \
  -w "\nHTTP %{http_code}\n" | tail -4
curl -s -X POST localhost:8001/v1/agents/$PLAN_AGENT/run -H 'content-type: application/json' \
  -d '{"input":"What is 6 hours in minutes?"}' --max-time 120 -o /tmp/s3-pinned-run.json -w "HTTP %{http_code}\n"
# teardown — restore the standard backend
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```