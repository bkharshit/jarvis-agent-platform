# S2 manual walkthrough — auth, multi-tenancy, BYOK credentials (ADR 0009/0006), verified live

A hands-on reproduction of the S2 acceptance tests against a live stack,
with the "why" and the concept behind each step. Verified 2026-09-06
against dev DB `jarvis`, backend on :8001.

One rule to keep in mind while reading (D29): **a tenant boundary reads as
404, never 403** — an id you can't see doesn't exist, so responses never
leak existence across tenants. And secrets are write-only: BYOK material
enters the API once (create/update) and no response, snapshot, or log ever
carries plaintext again.

New schema: `tenants`, `users`, `sessions`, `api_keys`, `credentials`, plus
`tenant_id` columns on `agents` / `agent_executions` / `conversations`
(migration 0004) and the `credential_ref` snapshot rewrite (migration 0005).

## 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN      # kill any stale backend first
uv run alembic upgrade head           # migrations 0004/0005: tenancy + credential_ref
# BYOK storage needs a master key: 32 random bytes, base64, in an env var
# whose NAME is JARVIS_CREDENTIALS_MASTER_KEY (D30):
python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > /tmp/mk
export JARVIS_CREDENTIALS_MASTER_KEY=$(cat /tmp/mk)
```

Anonymous is the default (`JARVIS_AUTH_MODE=anonymous`): local dev and the
CLI stay friction-free, and every Phase 1 workflow runs untouched.

## 1. Baseline — anonymous mode is unchanged

```bash
curl -s localhost:8001/healthz
curl -s localhost:8001/v1/capabilities | python3 -m json.tool | grep -A3 '"settings"'
# → "enabled": true, "detail": {"auth_mode": "anonymous", "credentials": {"available": true}}
curl -s localhost:8001/v1/agents | head -c 200        # 200, no credentials presented
```

**Proves** the anonymous default still works end to end, and the
capabilities payload is the fact the UI flipped on (the web Settings
section renders from exactly this payload — `enabled: true` since commit 8).

## 2. Required mode — 401s with the frozen envelope

```bash
JARVIS_AUTH_MODE=required JARVIS_PORT=8002 uv run jarvis serve &
curl -s localhost:8002/v1/agents
# → {"error":{"kind":"unauthenticated","message":"authentication required — log in or present an API key"}}
curl -s localhost:8002/healthz          # public, no auth
```

**Proves** auth is a config choice, not a rewrite: the same binary serves
anonymous local dev and required multi-user mode; `/healthz` and
`/v1/capabilities` stay public either way (the UI needs capabilities before
it knows how to sign in).

**Concept:** the `AuthContext` dependency (`api/auth.py`) resolves
credentials → `(Principal, user)`; presented-but-invalid credentials 401
even in anonymous mode (a wrong key must never silently fall back to
anonymous — that would mask revocation).

## 3. CLI bootstrap — the chicken-and-egg commands (ADR 0009 §9)

```bash
uv run jarvis tenant create acme "Acme Corp"
uv run jarvis user create acme owner@acme.test --role owner --password s3cret
uv run jarvis api-key create owner@acme.test --name cli
# → key: jarvis_sk_<64hex>  (printed exactly once)
```

**Proves** the first principal can exist without any authenticated route.
The CLI is an in-process platform consumer (D26) and stays anonymous — no
authenticated route can provision the principal that would authenticate it.

**Concept:** passwords are hashed (scrypt) and never echoed; API keys are
`jarvis_sk_<32hex>`, stored as SHA-256 hash + 12-char display prefix, the
plaintext returned exactly once.

## 4. Login → session cookie → whoami

```bash
curl -s -c /tmp/jarvis-jar -X POST localhost:8002/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@acme.test","password":"s3cret"}'
# → {"tenant_id":"acme","mode":"session","user_id":"…","email":"owner@acme.test","role":"owner"}

curl -s localhost:8002/v1/auth/whoami -b /tmp/jarvis-jar   # same principal
curl -s -X POST localhost:8002/v1/auth/logout -b /tmp/jarvis-jar   # 204, cookie cleared
```

**Proves** sessions are server-side rows; the cookie carries only an opaque
random token (its hash is stored — a stolen sessions table yields no
tokens). The cookie is httpOnly + SameSite=Lax.

## 5. API-key callers and tenant isolation

```bash
KEY=jarvis_sk_<from step 3>
curl -s localhost:8002/v1/agents -H "Authorization: Bearer $KEY" | head -c 120
# → {"items": [...only acme's agents...]}
```

Then, as a second tenant, try a foreign id:

```bash
uv run jarvis tenant create globex && \
uv run jarvis user create globex g@x.test --role owner --password p
# login as g@x.test, save its cookie, then:
curl -s localhost:8002/v1/agents/<an-acme-agent-id> -b /tmp/globex-jar
# → 404 {"error":{"kind":"not_found",...}}   — 404, not 403 (no existence leak)
```

**Proves** tenant scoping happens at the repository boundary (WHERE clauses
in the scoped views), and a foreign id is indistinguishable from a missing
one.

## 6. Member management — minimal roles, no RBAC

```bash
curl -s localhost:8002/v1/members -b /tmp/jarvis-jar           # list (owner)
curl -s -X POST localhost:8002/v1/members -b /tmp/jarvis-jar \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@acme.test","role":"member"}'               # 201
# as a member: 403 "member management requires the admin or owner role"
# a foreign member id → 404; PATCH/DELETE on the owner, as an admin → 403
# delete a member who owns API keys → 409 (audit rows reference the user)
```

**Proves** roles are exactly `owner | admin | member`: member management is
admin/owner-only; only owners act on owners (no admin-driven lockout);
password_hash never crosses the API.

## 7. BYOK credentials — write-only, encrypted at rest (ADR 0006)

```bash
curl -s -X POST localhost:8002/v1/credentials -b /tmp/jarvis-jar \
  -H 'Content-Type: application/json' \
  -d '{"name":"prod key","provider":"openai_compatible","secret":"sk-e2e-material-9876543210"}'
# → {"id":"<cred-id>","tenant_id":"acme","name":"prod key",...}  — no secret, no ciphertext

curl -s localhost:8002/v1/credentials -b /tmp/jarvis-jar | grep -c sk-e2e   # → 0
psql jarvis -c "SELECT ciphertext FROM credentials WHERE name='prod key';"
# → {"v":1,"key_id":...,"nonce":...,"ct":...}  — AES-GCM envelope, never plaintext
```

PATCH replaces the secret (fresh nonce each time), DELETE revokes (tombstone —
runs already in flight resolve-or-fail per snapshot). Without the master key
the routes answer 503 `credentials_unavailable` and write nothing; tampering
with the ciphertext fails the run loudly as a `model` failure, never silently.

## 8. A run bound to a stored credential — live, tenant-scoped (D28)

```bash
# bind an agent's model to the stored credential by id:
curl -s -X POST localhost:8002/v1/agents -b /tmp/jarvis-jar \
  -H 'Content-Type: application/json' \
  -d '{"name":"byok-agent","model":{"provider":"openai_compatible","model":"<model>","base_url":"https://<endpoint>/v1","credential_ref":{"type":"stored","credential_id":"<cred-id>"}},"system_prompt":"You are terse.","strategy":{"type":"function_calling"}}'

curl -s -N -X POST localhost:8002/v1/agents/<agent-id>/stream -b /tmp/jarvis-jar \
  -H 'Content-Type: application/json' -d '{"input":"Say hello."}'
# → events stream; the provider request carries "Authorization: Bearer sk-e2e-material-…"
```

**Proves** the resolution chain: the worker rebuilds the run from the queue
message alone (ADR 0008), resolves `credential_ref` through the tenant-scoped
`DatabaseStoredResolver`, decrypts AES-GCM, and materializes the client. The
secret appears exactly once — in the outbound Authorization header — and
nowhere in any transcript, event, or snapshot.

Now the isolation proof: an agent in tenant `globex` referencing acme's
credential id resolves to `CredentialError("credential ... not found")`
inside the runtime's try block — the run ends in a **persisted terminal
`run.failed` with `error_kind="model"`** (D5), not a worker claim failure.
This exact scenario found the D28 bug pre-fix: resolution outside the try
made the worker retry the message forever.

```bash
curl -s localhost:8002/v1/executions/<run-id> -b /tmp/globex-jar | python3 -c \
  "import json,sys; r=json.load(sys.stdin)['run']; print(r['status'], r['error_kind'], r['error'])"
# → failed model "credential '...' not found"
```

## 9. The web Settings section (UI-enablement item)

With the backend on :8001 and `npm run dev` in `web/`, the Settings section
is live: sign-in screen when `JARVIS_AUTH_MODE=required` and nobody is
acting; whoami summary, Members / API keys / Credentials panels when
signed in; anonymous mode says so out loud. The API-key plaintext is shown
exactly once with a "copy it now" banner; the credentials panel is
write-only (a secret is typed once, then only ever replaced).

```bash
cd web && JARVIS_API_URL=http://127.0.0.1:8001 npm run dev   # → http://localhost:5173/settings
```

**Concept:** the UI renders backend facts only — `capabilities.settings`
carries `auth_mode` and whether BYOK storage is configured (presence of the
master key, never its value), and every panel's error text is the API's own
message, verbatim.

## Appendix — full copy-paste validation sequence

A single end-to-end pass through every acceptance check. Assumes sections
0–8 above for the "why"; this is the hands-on script. Uses the live Ollama
Cloud model (`gemma4:31b` via `https://ollama.com/v1`, key value in
`./.env` as `OLLAMA_API_KEY`); where no network is wanted, substitute
`provider: mock` + `strategy: {type: function_calling}` and drop the
`credential_ref`.

**Paste-safety conventions used throughout:**

- Every create is **fire-and-forget** (`-o /dev/null`) — a duplicate name
  409s harmlessly on re-run — and the id is then **looked up by name**, so
  any block can be re-run in any order. The flip side: the POSTs hide
  *why* they failed. Swap in `-w '%{http_code}\n' -o /dev/null` to see
  the status — a 401 there usually means the cookie jar was logged out
  (section 3 clears `/tmp/acme.jar`); re-login and re-run the block.
- Two response shapes to know: agent create returns `AgentDetail`
  (`{definition, versions}` — id nested at `definition.id`), while
  credentials/members/api-keys return top-level `id`; execution list items
  are `RunResult` rows whose id field is `run_id`.
- `# →` lines are annotations, not part of the command.

### 0. Setup

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN; lsof -nP -iTCP:8002 -sTCP:LISTEN   # kill stale backends
uv run alembic upgrade head

# BYOK master key: 32 random bytes, base64, under the configured env-var NAME (D30)
python3 -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" > /tmp/mk
export JARVIS_CREDENTIALS_MASTER_KEY=$(cat /tmp/mk)

JARVIS_AUTH_MODE=required JARVIS_PORT=8002 uv run jarvis serve > /tmp/jarvis-8002.log 2>&1 &
```

### 1. Required mode 401s with the frozen envelope

```bash
curl -s localhost:8002/v1/agents
# → {"error":{"kind":"unauthenticated","message":"authentication required — log in or present an API key"}}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/healthz
# → 200 (public)
curl -s localhost:8002/v1/capabilities | python3 -m json.tool | grep -A4 '"settings"'
# → "enabled": true, "detail": {"auth_mode": "required", "credentials": {"available": true}}
```

### 2. CLI bootstrap (ADR 0009 §9)

```bash
uv run jarvis tenant create acme "Acme Corp"
uv run jarvis user create acme owner@acme.test --role owner --password s3cret
uv run jarvis api-key create owner@acme.test --name cli
export ACME_KEY=jarvis_sk_...   # copy the printed key NOW — never shown again
```

### 3. Session login → whoami → logout

```bash
curl -s -c /tmp/acme.jar -X POST localhost:8002/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"owner@acme.test","password":"s3cret"}'
# → {"tenant_id":"acme","mode":"session","user_id":"…","email":"owner@acme.test","role":"owner"}
curl -s localhost:8002/v1/auth/whoami -b /tmp/acme.jar
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8002/v1/auth/logout -b /tmp/acme.jar
# → 204
```

### 4. API-key caller + an env-ref agent run

```bash
curl -s localhost:8002/v1/agents -H "Authorization: Bearer $ACME_KEY" | head -c 120

curl -s -X POST localhost:8002/v1/agents -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d '{
    "name": "byok-walkthrough-env",
    "model": {"provider": "openai_compatible", "model": "gemma4:31b", "base_url": "https://ollama.com/v1",
              "credential_ref": {"type": "env", "env_var": "OLLAMA_API_KEY"}},
    "system_prompt": "You are terse.", "strategy": {"type": "function_calling"}}' -o /dev/null
# (201 on first run; a duplicate name 409s harmlessly)

AGENT=$(curl -s localhost:8002/v1/agents -H "Authorization: Bearer $ACME_KEY" \
  | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='byok-walkthrough-env'][0])")
echo "agent: $AGENT"

curl -s -N -X POST localhost:8002/v1/agents/$AGENT/stream -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d '{"input":"Reply with the single word: ok."}' | tail -4
# → …event: run.completed, "final_message": "ok"

RUN=$(curl -s "localhost:8002/v1/executions?agent_id=$AGENT" -H "Authorization: Bearer $ACME_KEY" \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['items'][0]['run_id'])")
curl -s localhost:8002/v1/executions/$RUN -H "Authorization: Bearer $ACME_KEY" \
  | python3 -c "import json,sys;r=json.load(sys.stdin)['run'];print(r['status'], r['error_kind'], r['error'])"
# → succeeded None None   (terminal event run.completed; row status "succeeded")
```

### 5. BYOK credential — write-only, encrypted at rest

```bash
export OLLAMA_KEY=$(grep '^OLLAMA_API_KEY=' .env | cut -d= -f2-)
echo "OLLAMA_KEY length: ${#OLLAMA_KEY}"
# → must be > 0; if 0 the export failed — fix .env or the grep before continuing

curl -s -X POST localhost:8002/v1/credentials -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"ollama byok\",\"provider\":\"openai_compatible\",\"secret\":\"$OLLAMA_KEY\"}" -o /dev/null
# (201 on first run; a duplicate name 409s harmlessly)

CRED=$(curl -s localhost:8002/v1/credentials -H "Authorization: Bearer $ACME_KEY" \
  | python3 -c "import json,sys;print([c['id'] for c in json.load(sys.stdin)['items'] if c['name']=='ollama byok'][0])")
echo "credential: $CRED"

curl -s localhost:8002/v1/credentials -H "Authorization: Bearer $ACME_KEY" | grep -c "$OLLAMA_KEY"
# → 0   (write-only: the secret never comes back)

psql jarvis -tAc "SELECT ciphertext FROM credentials WHERE id='$CRED';"
# → {"v":1,"key_id":…,"nonce":…,"ct":…}   — AES-GCM envelope, never plaintext

curl -s -X PATCH localhost:8002/v1/credentials/$CRED -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d "{\"secret\":\"$OLLAMA_KEY\"}" | head -c 160
# rotate the secret in place — fresh nonce per envelope
```

### 6. A run bound to the stored credential (the D28 resolution chain)

```bash
curl -s -X POST localhost:8002/v1/agents -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d '{
    "name": "byok-walkthrough-stored",
    "model": {"provider": "openai_compatible", "model": "gemma4:31b", "base_url": "https://ollama.com/v1",
              "credential_ref": {"type": "stored", "credential_id": "'"$CRED"'"}},
    "system_prompt": "You are terse.", "strategy": {"type": "function_calling"}}' -o /dev/null

BYOK_AGENT=$(curl -s localhost:8002/v1/agents -H "Authorization: Bearer $ACME_KEY" \
  | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='byok-walkthrough-stored'][0])")

curl -s -N -X POST localhost:8002/v1/agents/$BYOK_AGENT/stream -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d '{"input":"Reply with the single word: ok."}' | tail -4
# → …event: run.completed — the worker resolved the ref, decrypted AES-GCM, used the material
```

### 7. Tenant isolation — 404, never 403 (D29)

```bash
uv run jarvis tenant create globex
uv run jarvis user create globex g@x.test --role owner --password p
curl -s -c /tmp/globex.jar -X POST localhost:8002/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"g@x.test","password":"p"}' -o /dev/null

curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/v1/agents/$AGENT -b /tmp/globex.jar
# → 404
curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/v1/executions/$RUN -b /tmp/globex.jar
# → 404
curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/v1/credentials/$CRED -b /tmp/globex.jar
# → 404
```

The D5/D28 terminal-failure proof — a globex agent referencing acme's
credential id ends as a **persisted terminal `model` failure**, not a
worker claim-failure retry loop:

```bash
curl -s -X POST localhost:8002/v1/agents -b /tmp/globex.jar \
  -H 'Content-Type: application/json' -d '{
    "name": "bad-cred-agent",
    "model": {"provider": "openai_compatible", "model": "gemma4:31b", "base_url": "https://ollama.com/v1",
              "credential_ref": {"type": "stored", "credential_id": "'"$CRED"'"}},
    "system_prompt": "You are terse.", "strategy": {"type": "function_calling"}}' -o /dev/null

G_AGENT=$(curl -s localhost:8002/v1/agents -b /tmp/globex.jar \
  | python3 -c "import json,sys;print([a['id'] for a in json.load(sys.stdin)['items'] if a['name']=='bad-cred-agent'][0])")

curl -s -N -X POST localhost:8002/v1/agents/$G_AGENT/stream -b /tmp/globex.jar \
  -H 'Content-Type: application/json' -d '{"input":"hi"}' -o /dev/null

G_RUN=$(curl -s "localhost:8002/v1/executions?agent_id=$G_AGENT" -b /tmp/globex.jar \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['items'][0]['run_id'])")
curl -s localhost:8002/v1/executions/$G_RUN -b /tmp/globex.jar \
  | python3 -c "import json,sys;r=json.load(sys.stdin)['run'];print(r['status'], r['error_kind'], r['error'])"
# → failed model "credential '<cred-id>' not found"
```

### 8. Members & roles — minimal, no RBAC

```bash
curl -s -X POST localhost:8002/v1/members -b /tmp/acme.jar \
  -H 'Content-Type: application/json' -d '{"email":"dev@acme.test","role":"member","password":"devpass"}' -o /dev/null
# (201 on first run; a duplicate email 409s harmlessly)

MEMBER_ID=$(curl -s localhost:8002/v1/members -b /tmp/acme.jar \
  | python3 -c "import json,sys;print([u['id'] for u in json.load(sys.stdin)['items'] if u['email']=='dev@acme.test'][0])")
echo "member: $MEMBER_ID"

curl -s -c /tmp/dev.jar -X POST localhost:8002/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"dev@acme.test","password":"devpass"}' -o /dev/null

curl -s localhost:8002/v1/members -b /tmp/dev.jar
# → 403 {"error":{"kind":"forbidden","message":"member management requires the admin or owner role"}}

curl -s localhost:8002/v1/agents -b /tmp/dev.jar | head -c 80
# members CAN read tenant agents

curl -s -X POST localhost:8002/v1/api-keys -b /tmp/dev.jar \
  -H 'Content-Type: application/json' -d '{"name":"dev-key"}' | head -c 140
# and manage keys

curl -s -o /dev/null -w '%{http_code}\n' -X DELETE localhost:8002/v1/members/$MEMBER_ID -b /tmp/acme.jar
# → 409 (member owns keys — audit rows survive)
```

### 9. API-key revocation

```bash
NEW=$(curl -s -X POST localhost:8002/v1/api-keys -H "Authorization: Bearer $ACME_KEY" \
  -H 'Content-Type: application/json' -d '{"name":"temp-key"}')
KEY2=$(echo "$NEW" | python3 -c "import json,sys;print(json.load(sys.stdin)['plaintext'])")
KEY2_ID=$(echo "$NEW" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
# api-keys is the one create that returns top-level id + plaintext (exactly once)

curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/v1/agents -H "Authorization: Bearer $KEY2"
# → 200
curl -s -o /dev/null -w '%{http_code}\n' \
  -X DELETE localhost:8002/v1/api-keys/$KEY2_ID -H "Authorization: Bearer $ACME_KEY"
# → 204
curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/v1/agents -H "Authorization: Bearer $KEY2"
# → 401
```

### 10. Missing master key → honest 503 (optional)

Only meaningful if the key is not also set in `./.env`:

```bash
env -u JARVIS_CREDENTIALS_MASTER_KEY JARVIS_AUTH_MODE=required JARVIS_PORT=8003 \
  uv run jarvis serve > /tmp/jarvis-8003.log 2>&1 &
curl -s localhost:8003/v1/credentials -b /tmp/acme.jar
# → {"error":{"kind":"credentials_unavailable","message":"stored credentials are not configured: …"}}
kill %2
```

### 11. Teardown and the web UI

```bash
lsof -nP -iTCP:8002 -sTCP:LISTEN   # find the PID, then: kill <pid>
cd web && npm run dev              # → http://localhost:5173/settings
```