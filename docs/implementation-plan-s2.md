# JARVIS — Implementation Plan (Stage S2: auth, multi-tenancy & BYOK)

- **Status**: In progress
- **Date**: 2026-09-06
- **Rides on**: ADR 0006 (credential resolution — `CredentialResolver`
  port, write-only credentials, tenant-scoped authorization, the
  `credential_ref` migration), ADR 0008 (queue message = the run's trust
  boundary), ADR 0009 (this stage's contract record), D5 (a run never
  raises — credential failure is a terminal `model` failure), D13 (one
  error envelope), D18 (secrets referenced, never stored — survives for
  everything an agent persists), roadmap §S2.

## Context

The platform is currently single-tenant and anonymous: every route is
unauthenticated, repos are unscoped, and the only secret mechanism is
`api_key_env` (D18). S2 adds callers, password accounts + sessions, API
keys, tenant isolation, and BYOK credentials — then flips the Settings UI
on. Scope decisions taken with the user (2026-09-06):

1. **Password accounts too** — not API keys alone: users get email +
   scrypt-hashed passwords, server-side sessions (httpOnly cookie), login/
   logout, and member management; API keys serve programmatic access.
2. **AES-GCM + local master key** for stored credentials —
   `JARVIS_CREDENTIALS_MASTER_KEY` (base64 32 bytes); KMS stays deferred
   per ADR 0006.
3. **`credential_ref` replaces `api_key_env`** — one-shot snapshot data
   migration, exactly as ADR 0006's S2 section describes.

Verified seams this stage rides on (read in code, not from docs):

- `domain/execution.py` — `ExecutionContext` already carries `user_id`;
  gains `tenant_id`. `RunResult` persists it on run rows (row column
  already exists: `AgentExecutionRow.user_id`).
- `api/deps.py` — `AppContainer` is the single wiring point; a new auth
  dependency resolves `Authorization: Bearer …` / session cookie /
  anonymous to a `Principal`, plus per-request scoped repo views.
- `models/factory.py` — `resolve()` has exactly one runtime caller
  (`agent_runtime.py:116`); the factory builds a *fresh* provider per
  resolve, so resolved key material can live and die inside one
  provider instance (ADR 0006's short plaintext lifetime).
- `models/openai_compatible.py` — `_headers()` already supports both
  `api_key` (material) and `api_key_env` (lazy env read) — the resolver's
  two outcomes map onto what exists.
- `ports/queue.py` — `RunQueueMessage` is the run's trust boundary
  (ADR 0008): the worker never consults the requester, so `tenant_id`
  must ride the message.
- `api/routes/capabilities.py` — `settings` is stubbed
  `{"enabled": False, "stage": "S2"}`; the flip is data, not code.
- `web/src/capabilities/` — `SectionGate` renders enabled sections from
  the payload; `ComingSoon` is replaced by real Settings screens.

## Confirmed decisions

1. **Auth mode is a setting, not a rewrite: `JARVIS_AUTH_MODE`
   (`anonymous` default | `required`).** Anonymous resolves to a fixed
   `default` tenant with full access — every Phase 1 test and the local
   workflow are untouched. `required` 401s unauthenticated requests
   (envelope, D13). `/healthz` and `/v1/capabilities` stay public.
2. **Sessions are server-side rows, not JWTs** — revocation is a DELETE,
   expiry a column, no signature infra. Cookie `jarvis_session`, httpOnly,
   SameSite=Lax (mutating cross-site JSON POSTs fail CORS preflight, so
   no separate CSRF token in S2; revisited if a non-same-origin frontend
   ships). TTL 30 days fixed.
3. **Passwords: stdlib `hashlib.scrypt`** (n=2^14, r=8, p=1), per-user
   random salt, format-versioned string `scrypt$n$r$p$salt$hash`,
   `hmac.compare_digest` verify. No `passlib`/`bcrypt` dependency.
4. **API keys**: `jarvis_sk_<32hex>`; SHA-256 hash + 12-char display
   prefix stored; plaintext returned exactly once at create; a key
   belongs to a user and inherits their identity; revocation is a column.
5. **Roles are `owner | admin | member`** — member management requires
   admin/owner; keys and credentials are open to all members. Deliberately
   not RBAC.
6. **Scoping = scoped repo views at the boundary** (WHERE clauses, never
   post-filters; ADR 0009 §4). Ports stay untouched — the wrappers
   implement the same Protocols. `agents.tenant_id` NULL = platform-shared;
   executions/conversations NOT NULL. Cross-tenant → 404 (no existence
   leak). Nested resources (events/messages/tool executions) authorize via
   the scoped parent-run lookup — no tenant column on hot-path tables.
7. **`credential_ref` union replaces `api_key_env`** (ADR 0006): env refs
   stay lazy (D18 preserved — `ResolvedEnv` → `api_key_env=` on the
   provider); stored refs resolve to short-lived material
   (`ResolvedMaterial` → `api_key=`, alive only inside the per-resolve
   provider instance). Stored resolution without a master key configured →
   resolver unavailable, capabilities says so (honest gating).
8. **Crypto**: `cryptography`'s AESGCM; ciphertext JSONB
   `{v: 1, key_id, nonce, ct}`; `key_id` = 8-byte fingerprint of the
   master key (rotation-aware format, no re-wrap logic yet). Master key
   referenced by *env-var name* in Settings (D18 pattern) — the value
   never enters config.
9. **The CLI stays anonymous/in-process** (D26) and gains bootstrap
   commands (`tenant create`, `user create`, `api-key create`) because the
   first owner cannot be created through an authenticated route.

## Design

### Data model (migrations 0004 + 0005)

- `0004_tenancy`:
  - `tenants` — `id` (default: `default`), `name`, `created_at`.
  - `users` — `id`, `tenant_id` FK, `email` (unique), `display_name`,
    `password_hash` (nullable — a user may hold keys only), `role`
    (`owner|admin|member`, plain string + CHECK-free literal in code),
    `created_at`.
  - `sessions` — `id`, `user_id` FK, `token_hash` (unique),
    `expires_at`, `created_at`.
  - `api_keys` — `id`, `tenant_id` FK, `user_id` FK, `name`,
    `key_hash` (unique), `key_prefix`, `created_at`, `last_used_at`
    (nullable), `revoked_at` (nullable).
  - `credentials` — `id`, `tenant_id` FK, `name`, `provider`,
    `ciphertext` JSONB (`{v, key_id, nonce, ct}`), `created_by` FK,
    `created_at`, `updated_at`, `revoked_at` (nullable). **No plaintext
    column exists to leak.**
  - `agents.tenant_id` (nullable), `agent_executions.tenant_id`
    (NOT NULL, backfill `default`), `conversations.tenant_id` (NOT NULL,
    backfill `default`), each indexed. Backfill creates the `default`
    tenant row.
- `0005_credential_ref`: one-shot JSONB rewrite of
  `agent_versions.snapshot`: `model.api_key_env: "X"` →
  `model.credential_ref: {type: env, env_var: "X"}` (env references only;
  runs in the same transaction as the model-shape change in the domain —
  commit 3 keeps them atomic).

### Domain + ports

- `domain/agent.py`: `EnvCredentialRef{env_var}` /
  `StoredCredentialRef{credential_id}` (discriminated by `type`);
  `ModelRef.credential_ref: CredentialRef | None = None`;
  `api_key_env` **removed** (extra="forbid" makes stale writers fail
  loudly — ADR 0002).
- `domain/execution.py`: `ExecutionContext.tenant_id: str | None`.
- New `domain/auth.py`: `Principal{tenant_id, user_id, api_key_id?, role?,
  mode}` (pydantic; `mode: anonymous|session|api_key`).
- `ports/credential.py` (new): `ResolvedCredential = ResolvedEnv(env_var)
  | ResolvedMaterial(value)`; `CredentialResolver.resolve(principal, ref)
  -> ResolvedCredential` (raises `CredentialError`); `ports/__init__`
  exports. pydantic/stdlib only (dependency rule).
- `ports/model.py`: `ModelProviderFactory.resolve(ref, *, principal=None)`
  and `list_models(provider, *, base_url=None, credential_ref=None,
  principal=None)` — ADR 0006 §"Target architecture" sanctions this
  threading.
- `ports/queue.py`: `RunQueueMessage.tenant_id: str | None = None`.

### Adapters

- `security/` (new package):
  - `passwords.py` — scrypt hash/verify (`hash_password`, `verify_password`).
  - `api_keys.py` — `generate_api_key()` (`jarvis_sk_` + 32hex),
    `hash_api_key` (sha256 hex), `key_prefix` (display).
  - `crypto.py` — `load_master_key(settings)` (reads the env-var *name*),
    `encrypt(secret) -> dict`, `decrypt(payload) -> str`, `key_id()`
    fingerprint; AESGCM, 96-bit nonce, tamper fails loudly.
- `models/factory.py` — takes a `CredentialResolver`; `resolve` builds the
  provider with `api_key=` (material) or `api_key_env=` (env) per the
  resolved credential. `EnvCredentialResolver` (in `models/credentials.py`
  or `security/`): env ref → `ResolvedEnv`; stored ref → `CredentialError`
  ("stored credentials are not available in this deployment").
- `persistence/repositories.py`: `SqlAuthRepo` (users/sessions/api_keys/
  credentials CRUD; sessions by token_hash; keys by hash with
  last_used_at stamp; credentials tenant-scoped, ciphertext passthrough —
  it never sees plaintext).
- **Scoped views** (`persistence/scoped.py`): `TenantScopedAgents`,
  `TenantScopedExecutions`, `TenantScopedConversations` — thin wrappers
  binding `tenant_id`, structurally implementing the ports; agents'
  reads include `OR tenant_id IS NULL` (platform-shared); writes stamp the
  tenant.

### API

- `api/auth.py` (new): `AuthContext` dependency — resolve mode:
  1. `Authorization: Bearer jarvis_sk_…` → hash → api_keys row (not
     revoked) → `Principal(mode="api_key")` + `last_used_at` stamp;
  2. `jarvis_session` cookie → sessions row (unexpired) → user →
     `Principal(mode="session")`;
  3. anonymous mode → `Principal(tenant_id="default", mode="anonymous")`;
  4. `required` mode with neither → 401 envelope.
  Returns `AuthContext{principal, agents, executions, conversations,
  credentials}` — the per-request scoped repo bundle routes consume
  instead of `container.*`.
- Routes: all existing routes switch from `container.agents` etc. to the
  scoped views; `404` mapping unchanged for missing rows. New routers:
  - `auth`: `POST /auth/login` (sets cookie), `POST /auth/logout`,
    `GET /auth/whoami`.
  - `members`: list/create/patch/delete (admin/owner only).
  - `api-keys`: list (metadata only) / create (plaintext once) /
    delete (revoke).
  - `credentials`: list (metadata only) / create / patch (name+secret) /
    delete (revoke) — GET schemas have **no secret field** (ADR 0006 §7).
- `routes/capabilities.py`: `settings.enabled=True`; `detail` carries
  `{auth_mode, credentials: {available}}`; anonymous mode keeps a notice
  flag the UI renders honestly.
- `routes/models.py`: `GET /v1/models` passes optional `credential_id` +
  principal (tenant-scoped stored-credential listing).

### Runtime/worker

- Run routes stamp `principal.tenant_id` into `RunQueueMessage.tenant_id`;
  `ExecutionContext.tenant_id` set from the message in the worker; run row
  persists it (column already existed).
- `AgentRuntime.run` resolves the model client with
  `factory.resolve(agent.model, principal=ctx.principal)` — resolution
  failure raises `ModelError` → existing runtime catch → terminal
  `run.failed` with `error_kind="model"` (D5; envelope untouched).

### Web (UI enablement — the stage's last item)

- `web/src/auth/`: `useWhoami` query; API client attaches session cookie
  (same-origin) and handles 401 → redirect to login; a `LoginScreen`.
- `web/src/sections/settings/`: `SettingsHome` (whoami summary,
  anonymous-mode notice when `auth_mode=anonymous`), `MembersPanel`,
  `ApiKeysPanel` (plaintext shown exactly once at create), `CredentialsPanel`
  (BYOK create/update/revoke; secret never returned).
- `sectionRegistry`/routes register the live Settings section; the shell
  drops the global anonymous-mode notice; openapi regen
  (`make gen-api`).

## Repository layout (end state of S2)

```
src/jarvis/
  domain/auth.py                  # Principal                              (new)
  domain/agent.py                 # CredentialRef union on ModelRef        (mod)
  domain/execution.py             # ExecutionContext.tenant_id             (mod)
  ports/credential.py             # CredentialResolver, ResolvedCredential (new)
  ports/model.py                  # resolve(list_models) principal param   (mod)
  ports/queue.py                  # RunQueueMessage.tenant_id              (mod)
  security/{passwords,api_keys,crypto}.py                                   (new)
  persistence/models.py           # Tenant/User/Session/ApiKey/CredentialRow (mod)
  persistence/repositories.py     # SqlAuthRepo                            (mod)
  persistence/scoped.py           # tenant-scoped repo views               (new)
  models/credentials.py           # EnvCredentialResolver                  (new)
  models/factory.py               # resolver injection + principal          (mod)
  api/auth.py                     # AuthContext dependency                 (new)
  api/routes/{auth,members,api_keys,credentials}.py                        (new)
  api/routes/*                    # scoped views, capabilities flip        (mod)
  cli/main.py                     # tenant/user/api-key bootstrap          (mod)
  config.py                       # auth_mode, credentials_master_key_env  (mod)
  persistence/migrations/versions/0004_tenancy.py, 0005_credential_ref.py
web/src/auth/*                    # whoami, login, client 401 handling     (new)
web/src/sections/settings/*       # Settings screens                        (new)
web/openapi.json, schema.d.ts     # regen
```

## Commit sequence (each independently green)

| # | Commit | Gates |
|---|--------|-------|
| 0 | `docs(adr): ADR 0009 + S2 implementation plan` | docs only |
| 1 | `feat(persistence): tenants/users/sessions/api_keys/credentials + tenant_id columns (migration 0004)` | unit + migration integration |
| 2 | `feat(domain): Principal + credential_ref union + snapshot data migration (0005)` | unit + integration |
| 3 | `feat(ports): CredentialResolver port + env resolver + factory principal threading` | unit |
| 4 | `feat(security): scrypt passwords, api-key hashing, AES-GCM credential crypto` | unit |
| 5 | `feat(persistence): SqlAuthRepo` | unit + integration |
| 6 | `feat(api): AuthContext dependency, scoped repo views, auth routes` | unit + **full integration suite** (Phase 1 tests pass unchanged under anonymous mode) |
| 7 | `feat(api): members/api-keys/credentials routes + secret-leak security tests` | unit + integration |
| 8 | `feat(runtime): tenant threading through queue/worker + tenant-isolation e2e` | unit + integration |
| 9 | `feat(cli): tenant/user/api-key bootstrap commands` | unit |
| 10 | `feat(web): Settings section enablement (login, members, keys, BYOK credentials)` | tsc, eslint, vitest |
| 11 | `docs(s2): decisions D28+, walkthrough, CLAUDE.md rule 5, README` | docs only |

Every commit: `pytest tests/unit`, `ruff check src tests`, `mypy src`
green before committing; the full integration suite green before commits
6–8; web gates green on commit 10.

## Risks & deliberate deferrals

- **Snapshot rewrite must never introduce plaintext** — the 0005
  migration only rewrites env-var *names*; the security test suite asserts
  no submitted secret appears in any GET, snapshot, or log (ADR 0006 §7).
- **Master key misconfiguration** — `credentials` capability reports
  unavailable rather than failing routes; encrypt/decrypt errors fail
  loudly (tamper = error, never silent).
- **Session fixation/CSRF** — fresh token at login, httpOnly + SameSite=Lax;
  no public registration (members are admin-created). Revisit only when a
  cross-origin frontend ships.
- **Deferred**: KMS/managed key storage (ADR 0006 deferral stands),
  credential *validation* endpoints (live provider check) and usage audit
  events (ADR 0006 deferrals), RBAC beyond three roles, session sliding
  expiry, invitation flows, `list_models` via stored credentials from the
  UI is supported at the API level but gets no dedicated editor wiring
  until the Models section gains write ability.

## Verification (S2 exit criteria)

1. `pytest tests/unit`, `ruff check src tests`, `mypy src` green; full
   integration suite green (Phase 1 behavior preserved under anonymous
   mode).
2. Acceptance (roadmap §S2): a tenant cannot read or run another tenant's
   agents or executions — **404, not 403**; anonymous mode remains a
   config choice; every Phase 1 test passes with a single shared tenant.
3. Security suite: no submitted plaintext secret appears in any GET
   response, agent-version snapshot, or log record (ADR 0006 §7);
   stored resolution is tenant-scoped (a credential id from another
   tenant resolves to "not found").
4. Manual walkthrough (docs/walkthrough-s2.md): `JARVIS_AUTH_MODE=required`
   end-to-end — bootstrap tenant/owner via CLI, login through the UI,
   create a BYOK credential, run an agent that uses it (env fallback for
   self-hosted), verify cross-tenant 404s and key revocation.
5. Web: `tsc --noEmit`, eslint, vitest green; Settings section renders
   real API state only (no fake functionality — decision 1.6).