# ADR 0013 — MCP credential headers

- **Status**: Accepted (built 2026-09-09)
- **Amends**: ADR 0012 §3 (`headers`/`env` widen from
  `dict[str, EnvCredentialRef]` to `dict[str, CredentialRef]`); rides
  ADR 0006 (the credential store it reuses, unchanged), ADR 0005
  (env-var indirection, unchanged), ADR 0012 §4/D38 (connect-time
  resolution inside the runtime's try, unchanged). Nothing in `ports/`
  changes.

## Context

Every serious public MCP http server authenticates with a header
(`Authorization: Bearer …`, `X-Api-Key: …`). ADR 0012 shipped header
support, but only the env half of the `CredentialRef` union: a user adding
`https://news-search-mcp.webz.io/mcp` from the UI had nowhere to put the
key — the curl-only answer was setting a process env var and hand-writing
a config. The stored half of ADR 0006 already exists for exactly this
shape: AES-GCM-encrypted, tenant-owned, write-only through the API.

## Decision

### 1. The ref union widens; nothing else in the config changes

`McpHttpConfig.headers` and `McpStdioConfig.env` widen from
`dict[str, EnvCredentialRef]` to `dict[str, CredentialRef]` — the existing
`env | stored` union from `domain/agent.py` (discriminator `type`,
`extra="forbid"`). Pure pydantic widening of JSONB: the discriminator gains
a variant, **no migration**; existing rows (env refs) parse unchanged. A
stored ref carries only the credential *id*:

```jsonc
{"type": "http", "url": "https://news-search-mcp.webz.io/mcp",
 "headers": {"Authorization":
   {"type": "stored", "credential_id": "<id from POST /v1/credentials>"}}}
```

Mixing variants in one server's headers is fine. The API echoes config
(ids and env-var names only — safe); the secret never enters the config,
a snapshot, a log line, or a response.

### 2. Resolution stays at connect time, inside `McpServerConnection`

The secret materializes **only** in `McpServerConnection._build_client`,
per segment, via the `CredentialResolver` already built for model
credentials. `McpToolProvider` gains an injected `credential_resolver`
(the same `DefaultCredentialResolver` instance `AppContainer` shares with
the model factory) and threads a `principal` per call:
`resolve(bindings, tenant_id, principal)` / `probe(server, principal)` /
`_factory_for(principal)` — the default connection factory closes over the
principal; custom `connection_factory` fakes keep their
`Callable[[McpServer], …]` signature untouched.

Env refs resolve from `os.environ` exactly as ADR 0012 shipped. Stored
refs resolve `await credential_resolver.resolve(principal, ref)` →
`ResolvedMaterial.value`, passed RAW as the header value (a Bearer token
must arrive with its `Bearer ` prefix). Three failure modes, all
`McpResolutionError` so the existing honest paths fire unchanged (probe →
502 `mcp_unreachable`; run → persisted terminal `run.failed`
`error_kind="tool"`, D38):

- provider built without a resolver → "…cannot be resolved — this
  deployment has no credential resolver" (a short-circuit, never an
  AttributeError);
- `CredentialError` → names the credential id and the header it was
  resolving for;
- the stored path somehow resolving to `ResolvedEnv` → contract-bug error
  (an env-var *name* must never ride through as a header value).

**Rejected alternative** (recorded per rule 7): the provider
pre-materializing `dict[str, str]` into the connection constructor. It
breaks every existing `connection_factory` test fake, keeps a plaintext
dict alive for the segment's lifetime instead of microseconds, and needs a
second CredentialError→502 mapping at the provider — for no gain, since
the connection already owns resolution.

### 3. Storage stays signed-in-only (Harshit's call, 2026-09-09)

The credentials create route keeps its 403 when `auth.user is None`, and
`created_by` stays NOT NULL — **anonymous mode never gains stored-secret
UI**. The UI gates the stored option on
`whoami.mode !== "anonymous" && settingsFacts(capabilities).credentialsAvailable`
and renders the option **absent, never disabled** (rule 6), with one honest
hint line pointing anonymous users at env vars. Anonymous-mode runs that
reference an existing stored ref (created while signed in) still resolve:
the principal exists at run time; the 403 is a *creation* boundary.

Scope: add + edit headers + rotate secret, all from the Tools page.
Rotation reuses the write-only PATCH (`{secret}` sent once, never echoed)
with a confirm that says the old value stops working immediately. Editing
a server's refs PATCHes the **full config** — `PATCH /v1/mcp/servers/{id}`
replaces `config` wholesale, so a refs-only patch would wipe `url`/`command`/`args`.

### 4. What does not change

The failure paths, envelope, event types, `ports/` contracts, probe
semantics (connects fresh, persists nothing, carries the principal now),
and D37's snapshot-vs-reference model: a version snapshot pins the tool
*name*; a stored credential being rotated or revoked degrades exactly like
any other resolution failure — the next segment's resolve is a terminal
`run.failed` `error_kind="tool"` naming the credential id. The
tenant-scoped-not-user-scoped credential store is intentional per
ADR 0006 — a rotation is a tenant-level event affecting every agent
referencing the id.

## Consequences

- **No migration.** Config is typed JSONB (ADR 0002); the union widening
  is backward- and forward-compatible at the value level.
- **The resolver is load-bearing for MCP now** — `McpToolProvider` without
  a `credential_resolver` still works for env-only configs and fails with
  a clear message on stored refs (unit-tested; the runtime test proves the
  persisted-terminal path).
- **UI** — the add-server dialog gains a headers section (http transport)
  with the credential picker and inline `provider: "mcp_header"` credential
  creation; server rows gain edit-refs forms and per-ref rotate buttons.
  `make gen-api` widened `headers`/`env` in the generated schema.
- **Deferred**: OAuth flows (static refs only, as ADR 0012 deferred);
  per-user credential scoping; header *value* templating beyond the raw
  resolved string.