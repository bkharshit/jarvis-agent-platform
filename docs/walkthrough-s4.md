# S4 manual walkthrough — MCP tool providers (D37/D38), verified live

A hands-on reproduction of the S4 acceptance against a live stack, with
the "why" behind each step. Verified 2026-09-08 (API sections run live
in the build session; §6 UI pass is the manual-testing session) against
dev DB `jarvis`,
backend on :8001, real model `gemma4:31b` via Ollama Cloud (BYO key in
`.env`, referenced by *name* — never pasted into config, D29/D30), and a
REAL stdio MCP server: `tests/fixtures/mcp/server.py` (echo +
add_numbers), launched by the platform as a subprocess. Only the
transport is real; the fixture server is ours, so the walkthrough needs
no third-party network.

Three rules to keep in mind while reading:

- **D37 — servers are tenant-scoped registry rows; agents bind tools BY
  NAME.** `mcp__<server>__<tool>` is the join key from immutable version
  snapshots, so a server name never moves and a delete leaves agents'
  bindings intact — the next run fails resolution *honestly* (§5).
  Discovery is not exposure: only BOUND tools ever register.
- **D38 — resolution is eager, per segment, inside the runtime's try**
  (the D28 pattern, third application). A missing/disabled/unreachable
  server is a persisted terminal `run.failed` with the existing
  `error_kind="tool"` naming the server — never a 500, never a retry
  loop. Per-call failures stay recoverable error ToolResults.
- **Approval is the default.** Every MCP descriptor carries
  `requires_approval: true` (ADR 0012 §2); a binding's
  `config.requires_approval: false` un-gates it (S10 binding-wins). §4
  rides the pause→decisions→resume chain end to end.

No new environment variables — S4 needs nothing in `.env`. A restart is
only needed to pick up the new code.

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale (pre-S4) backend first
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
uv run alembic upgrade head           # dev DB needs migration 0007 (mcp_servers)
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
```

**The migration matters**: until 0007 is applied, every `/v1/capabilities`
request 500s — the capabilities route reads the registry rows as a
derived fact, so the missing table surfaces immediately (found live in
this walkthrough; the integration suite self-migrates and cannot see it).

The MCP python SDK (`mcp>=2.0,<3`) is a main dependency — `uv sync`
already has it; the fixture server runs under the project venv:

```bash
ls .venv/bin/python tests/fixtures/mcp/server.py   # both must exist
```

## 1. The capabilities fact the UI flips on

```bash
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A8 '"mcp"'
# → "mcp": { "enabled": true, "servers": [] }        (no servers yet)
```

**Proves** the UI-enablement gate (decision 1.6): the Tools page's MCP
panel and the agent editor's MCP picker read this payload. It flips from
the S3-era coming-soon copy to the live panel with zero web-specific
gating code — and `servers` here is a *derived fact* (a listing of the
registry rows), not configuration.

## 2. Register the fixture server — config carries NAMES only

```bash
curl -s -X POST localhost:8001/v1/mcp/servers \
  -H 'content-type: application/json' \
  -d '{"name":"fixtures",
       "config":{"type":"stdio","command":"'"$PWD"'/.venv/bin/python",
                 "args":["'"$PWD"'/tests/fixtures/mcp/server.py"]}}' \
  -o /tmp/s4-server.json -w "HTTP %{http_code}\n"
python3 -m json.tool < /tmp/s4-server.json
# → HTTP 201 — id, name "fixtures", config.type stdio, enabled: true
```

(Already run once? A duplicate name is a **409 conflict** — the row
still exists; grab its id with `curl -s localhost:8001/v1/mcp/servers`.)

**Proves** the create boundary: the name is a strict slug (it becomes
part of every tool name `mcp__fixtures__*`, D37), the config union
validates at the domain edge (a bad `type` or an `ftp://` URL is a 422),
and nothing here can carry a secret — env refs are *names*, resolved
from the process env at connect time and never stored.

## 3. Probe: connect fresh, list tools, persist nothing

```bash
SERVER_ID=$(python3 -c "import json;print(json.load(open('/tmp/s4-server.json'))['id'])")
curl -s -X POST localhost:8001/v1/mcp/servers/$SERVER_ID/probe \
  --max-time 60 -o /tmp/s4-probe.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s4-probe.json'))
for t in d['tools']:
    print(t['name'], '| approval:',
          t['annotations']['requires_approval'], '|', t['description'])
EOF
# → mcp__fixtures__echo        | approval: True | Echo the given text back.
#   mcp__fixtures__add_numbers | approval: True | Add two numbers.
```

**Proves** the real stdio boundary: the platform spawned the fixture
subprocess, performed the MCP initialize handshake, ran tools/list, and
mapped the result to JARVIS descriptors with full names
(`mcp__<server>__<tool>`) and the approval default riding every one.
The probe connects fresh each time and persists nothing — try it on a
dead command (`"command":"definitely-not-a-real-command-xyz"`) and the
same route answers **502 `mcp_unreachable`** naming the server.

## 4. The live run: bind by name, approve through the pause

The agent binds the tool by NAME — no connection config in the
definition ever (credential_ref precedent, D37):

```yaml
# /tmp/mcp-agent.yaml
name: mcp-add-agent
description: Adds numbers through the fixtures MCP server (S4 walkthrough).
model:
  provider: openai_compatible
  model: gemma4:31b
  base_url: https://ollama.com/v1        # pin the endpoint (D28 gotcha)
  credential_ref:
    type: env
    env_var: OLLAMA_API_KEY
system_prompt: >-
  You are terse. Use the add_numbers tool for any arithmetic.
strategy:
  type: function_calling
tools:
  - name: mcp__fixtures__add_numbers
    config:
      requires_approval: true            # the default, made explicit
memory:
  enabled: false
  max_messages: 20
```

```bash
uv run jarvis agent create --file /tmp/mcp-agent.yaml
MCP_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c \
  "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='mcp-add-agent'][0])")
echo "agent: $MCP_AGENT"
```

Run it. The call is gated, so the run **pauses** — a human approves:

```bash
curl -s -X POST localhost:8001/v1/agents/$MCP_AGENT/run \
  -H 'content-type: application/json' -d '{"input":"What is 12 plus 30?"}' \
  --max-time 420 -o /tmp/s4-run.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s4-run.json'))
print(d['status'], '| run:', d['run_id'])
open('/tmp/s4-run-id','w').write(d['run_id'])
EOF
# → awaiting_input | run: <run_id>
```

Read the pending call id from the pause frame, then approve it:

```bash
RUN_ID=$(cat /tmp/s4-run-id)
CALL_ID=$(curl -s localhost:8001/v1/executions/$RUN_ID/events | python3 -c "
import json,sys
for e in json.load(sys.stdin)['events']:
    if e['event']['type']=='run.awaiting_input':
        print(e['event']['pending_calls'][0]['id'])")
echo "pending call: $CALL_ID"
curl -s -X POST localhost:8001/v1/executions/$RUN_ID/resume \
  -H 'content-type: application/json' \
  -d '{"decisions":{"'"$CALL_ID"'":true}}' \
  --max-time 420 -o /tmp/s4-resume.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s4-resume.json'))
print(d['status'], '| final:', d.get('final_message'))
EOF
# → succeeded | final: 42 (the model's own words — the sum came from the tool)
```

The tool execution is the receipt that the call really crossed the
stdio boundary twice (request in, result back):

```bash
curl -s localhost:8001/v1/executions/$RUN_ID | python3 -c "
import json,sys
d = json.load(sys.stdin)
t = d['tool_executions'][0]
print('tool:', t['tool_name'], '| output:', t['output'], '| is_error:', t['is_error'])"
# → tool: mcp__fixtures__add_numbers | output: 42.0 | is_error: False
```

**Proves** the whole chain: discovery-by-binding (the registry view had
exactly one MCP tool), the approval default pausing the run (S10's
pause route), the resume segment re-resolving the MCP connection fresh
(D38 — connections close with the segment; resume re-resolves), and the
rows/events shape identical to a builtin run (gapless sequences across
the resume boundary — the integration suite asserts this exactly).

Approving with `false` instead (`"decisions":{"$CALL_ID":false}`) is
the refusal path: the tool is never called, the model gets the refusal
tool message, and the run finishes without the tool's answer.

## 5. Delete the server: the snapshot outlives it, honestly

No re-binding ceremony — the binding is in the published version
snapshot (immutable, D1), so deleting the row just pulls the ground out:

```bash
curl -s -X DELETE localhost:8001/v1/mcp/servers/$SERVER_ID -w "HTTP %{http_code}\n"
# → HTTP 204
curl -s -X POST localhost:8001/v1/agents/$MCP_AGENT/run \
  -H 'content-type: application/json' -d '{"input":"What is 2 plus 2?"}' \
  --max-time 120 -o /tmp/s4-deleted-run.json -w "HTTP %{http_code}\n"
python3 - <<'EOF'
import json
d = json.load(open('/tmp/s4-deleted-run.json'))
print(d['status'], '| error_kind:', d.get('error_kind'))
print('error:', d.get('error'))
EOF
# → failed | error_kind: tool
#   error: MCP server 'fixtures': no configured server with this name
#          (bound tool would never resolve)
```

Note the HTTP status is **200** again: a failed run is a valid
response. The failure is the persisted terminal event (exactly one
`run.failed`), the worker acked, and the model was never invoked —
resolution fails before a single token is spent (D38).

## 6. Teardown and the web UI

Recreate the server row (the §4/§5 runs reference it in history; the
Tools page reads live rows, so bring it back for the UI pass):

```bash
curl -s -X POST localhost:8001/v1/mcp/servers \
  -H 'content-type: application/json' \
  -d '{"name":"fixtures",
       "config":{"type":"stdio","command":"'"$PWD"'/.venv/bin/python",
                 "args":["'"$PWD"'/tests/fixtures/mcp/server.py"]}}' \
  -o /dev/null -w "HTTP %{http_code}\n"
```

Open http://localhost:5173 and:

- **Tools page** — the MCP panel is live under the builtins: the
  `fixtures` row with a `stdio` transport badge and its enabled state.
  "View tools" runs the probe and renders descriptor cards exactly like
  the builtin listing (§3's output, in the browser). Enable/disable
  toggles and Remove (with the confirm that says a delete means bound
  agents fail resolution) are admin/owner controls — in anonymous mode
  you have them all.
- **Add server dialog** — stdio (command + comma-separated args) or
  http (URL). Create a throwaway one, watch it appear, remove it.
- **Agent editor** — "Add MCP tool" lists the servers from
  capabilities; picking one probes it and its tools appear as
  checkboxes. Checking one adds an ordinary binding row with a
  **Requires approval** toggle defaulting ON — un-gating is an explicit
  choice, never a missing checkbox.
- **Executions** — the §4 run replays: user → assistant (tool request)
  → pause frame → tool result `42.0` → final message; the §5 failure
  renders as a red `run.failed` row with `error_kind: tool` naming
  `fixtures`.

## 7. Stored header credentials (ADR 0013) — the signed-in pass

Post-build addition (2026-09-09): header refs widen to the full
`CredentialRef` union — an http server's header can point at a *stored*
BYOK credential instead of an env var (D39). Resolution stays at connect
time inside `McpServerConnection`; failures reuse §5's honest paths. This
section needs a signed-in stack: master key + `JARVIS_AUTH_MODE=required`
(anonymous deployments keep env-var refs; the credentials create route
403s without a signed-in user, unchanged).

```bash
export JARVIS_CREDENTIALS_MASTER_KEY_NAME="JARVIS_CREDENTIALS_MASTER_KEY"
export JARVIS_CREDENTIALS_MASTER_KEY="$(openssl rand -base64 32)"   # fresh key = old creds unreadable
export JARVIS_AUTH_MODE=required
# restart the backend, then log in and take the session cookie through the steps below
```

1. **Create the credential (write-only):**
   `curl -X POST localhost:8001/v1/credentials -H 'content-type:
   application/json' -d '{"name":"webz-key","provider":"mcp_header",
   "secret":"Bearer sk_live_demo"}'` → 201 with metadata only; the secret
   is never returned again.
2. **Register a header-auth http server with a stored ref:**
   `POST /v1/mcp/servers` with
   `"headers":{"Authorization":{"type":"stored","credential_id":"<id>"}}`
   (an env ref rides alongside: `"X-Api-Key":{"type":"env","env_var":"WEBZ_MCP_TOKEN"}`).
3. **GET shows ids, never the secret.** Probe it → 502
   `mcp_unreachable` naming nothing about the credential when the
   endpoint is dead — resolution *passed*, the connection failed.
4. **Rotate the wrong value in** (Tools page → row → Rotate secret, or
   `PATCH /v1/credentials/<id>` `{secret}`) → probe 502 naming the
   credential id. Restore the right value → probe green.
5. **UI, signed-in:** the Add-server http form's headers section has the
   credential picker + inline create (`provider: "mcp_header"`); the row
   renders `Authorization → webz-key` with Rotate; "Edit headers" PATCHes
   the FULL config (a refs-only patch would wipe the URL — the msw echo
   test guards this).
6. **UI, anonymous:** the stored option is *absent* (never disabled) with
   the env-vars hint; no New-credential button.
7. **Revoke the credential** → the next run of an agent bound to that
   server is the §5 shape: terminal `run.failed`, `error_kind="tool"`,
   naming the credential id.

## Appendix — copy-paste-safe shell (the S2/S3 lesson)

If `JARVIS_DATABASE_URL` is not set in the shell, prefix it:

```bash
export JARVIS_DATABASE_URL="postgresql://jarvis:jarvis@localhost/jarvis"
```

The full sequence, in one block:

```bash
# §0 — restart on S4 code (no new env vars; DB needs 0007)
kill $(lsof -tiTCP:8001 -sTCP:LISTEN); sleep 1
uv run alembic upgrade head
uv run jarvis serve > /tmp/jarvis-8001.log 2>&1 &
sleep 3; curl -s localhost:8001/healthz
# §1 — capabilities
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A8 '"mcp"'
# §2 — register the fixture server
curl -s -X POST localhost:8001/v1/mcp/servers -H 'content-type: application/json' \
  -d '{"name":"fixtures","config":{"type":"stdio","command":"'"$PWD"'/.venv/bin/python","args":["'"$PWD"'/tests/fixtures/mcp/server.py"]}}' \
  -o /tmp/s4-server.json -w "HTTP %{http_code}\n"
SERVER_ID=$(python3 -c "import json;print(json.load(open('/tmp/s4-server.json'))['id'])")
# §3 — probe
curl -s -X POST localhost:8001/v1/mcp/servers/$SERVER_ID/probe --max-time 60 | python3 -m json.tool
# §4 — the agent (create the YAML above first), run, approve, resume
uv run jarvis agent create --file /tmp/mcp-agent.yaml
MCP_AGENT=$(curl -s localhost:8001/v1/agents | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='mcp-add-agent'][0])")
curl -s -X POST localhost:8001/v1/agents/$MCP_AGENT/run -H 'content-type: application/json' \
  -d '{"input":"What is 12 plus 30?"}' --max-time 420 -o /tmp/s4-run.json -w "HTTP %{http_code}\n"
RUN_ID=$(python3 -c "import json;print(json.load(open('/tmp/s4-run.json'))['run_id'])")
CALL_ID=$(curl -s localhost:8001/v1/executions/$RUN_ID/events | python3 -c "
import json,sys
for e in json.load(sys.stdin)['events']:
    if e['event']['type']=='run.awaiting_input':
        print(e['event']['pending_calls'][0]['id'])")
curl -s -X POST localhost:8001/v1/executions/$RUN_ID/resume -H 'content-type: application/json' \
  -d '{"decisions":{"'"$CALL_ID"'":true}}' --max-time 420 -o /tmp/s4-resume.json -w "HTTP %{http_code}\n"
python3 -c "import json;d=json.load(open('/tmp/s4-resume.json'));print(d['status'],'|',d.get('final_message'))"
# §5 — delete the server, run again: honest terminal tool failure
curl -s -X DELETE localhost:8001/v1/mcp/servers/$SERVER_ID -w "HTTP %{http_code}\n"
curl -s -X POST localhost:8001/v1/agents/$MCP_AGENT/run -H 'content-type: application/json' \
  -d '{"input":"What is 2 plus 2?"}' --max-time 120 -o /tmp/s4-deleted-run.json -w "HTTP %{http_code}\n"
python3 -c "import json;d=json.load(open('/tmp/s4-deleted-run.json'));print(d['status'],d.get('error_kind'),d.get('error'))"
# §6 — recreate for the UI pass, then browse
curl -s -X POST localhost:8001/v1/mcp/servers -H 'content-type: application/json' \
  -d '{"name":"fixtures","config":{"type":"stdio","command":"'"$PWD"'/.venv/bin/python","args":["'"$PWD"'/tests/fixtures/mcp/server.py"]}}' \
  -o /dev/null -w "HTTP %{http_code}\n"
```