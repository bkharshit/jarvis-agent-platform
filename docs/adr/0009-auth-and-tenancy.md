# ADR 0009 — Auth & multi-tenancy: principal model, repo scoping, and the auth surface

- **Status**: Accepted
- **Date**: 2026-09-06
- **Implements**: roadmap §S2; rides on ADR 0006 (credential resolution —
  the `CredentialResolver` port and the principal-threading through
  `resolve()` were decided there and land in this stage)

## Context

Every API route, repo, and run today is anonymous and unscoped: one shared
namespace, no caller identity. Phase 1 anticipated this —
`ExecutionContext.user_id` is persisted on runs, `agents` was left without
an owner column, and the API is a thin transport so a FastAPI dependency is
the only injection point. S2 adds callers, API keys, password accounts,
tenant isolation, and BYOK credentials (ADR 0006) in one stage.

## Decision

1. **`Principal` is a domain type; auth is a transport concern resolved to
   it.** A `Principal` (tenant_id, user_id, api_key_id?, mode) is built by
   an FastAPI dependency from whichever credential the request carries and
   then flows through repos, routes, the queue message, and
   `ExecutionContext`. Nothing above the API layer knows about HTTP, and
   nothing below the dependency knows about passwords or cookies.

2. **Two credential kinds authenticate requests: sessions and API keys.**
   - *Password accounts*: users carry email + scrypt-hashed passwords
     (stdlib `hashlib.scrypt`, per-user salt, format-versioned hash string —
     no new hashing dependency). Login creates a **server-side session**
     (random token, SHA-256 hash at rest, expiry row) delivered as an
     httpOnly `SameSite=Lax` cookie; logout revokes the row. Server-side
     sessions over JWT: revocation is a DELETE, expiry is a column, and no
     signature infrastructure is added.
   - *API keys*: `jarvis_sk_<32 hex>`; only the SHA-256 hash and a display
     prefix are stored. A key belongs to a user; the key's principal
     carries that user's identity. Plaintext is returned exactly once at
     creation and never again (ADR 0006 §7's write-only pattern, applied to
     keys).

3. **Auth mode is a config choice: `JARVIS_AUTH_MODE=anonymous|required`.**
   `anonymous` (the default) resolves every request to a fixed default
   tenant with full access — local dev, the CLI, unit tests, and every
   Phase 1 test keep working unchanged. `required` rejects unauthenticated
   requests with 401 in the standard error envelope (D13). `/healthz` and
   `GET /v1/capabilities` stay public in both modes — the pre-login shell
   must render from capabilities.

4. **Tenant scoping happens at the repository boundary as WHERE clauses,
   never post-filters — and the ports stay unchanged.** Each repo gains a
   scoped *view* (a thin wrapper binding a tenant, structurally
   implementing the same `AgentRepo`/`ExecutionRepo`/`ConversationRepo`
   Protocols). `agents.tenant_id` is **nullable: NULL = platform-shared**
   (visible to every tenant; single-tenant/anonymous agents are shared).
   `agent_executions` and `conversations` carry a NOT-NULL `tenant_id`
   (backfilled `default`). Cross-tenant reads return `None`/empty and
   routes map that to **404, not 403** — no existence leak.

5. **Nested resources derive authorization from their scoped parent.**
   Messages, tool executions, and execution events are reached only via
   `run_id` or `conversation_id`; their routes first load the parent run
   through the scoped repo (404 on foreign tenant) and then stream/query by
   that id. No tenant column is denormalized onto the hot-path event table;
   one indexed lookup gates them all.

6. **Tenant identity rides the queue message (ADR 0008's trust boundary).**
   `RunQueueMessage` gains `tenant_id`; the run route stamps the
   authenticated principal's tenant at enqueue; the worker rebuilds
   `ExecutionContext` from the message alone (it never consults the
   requester). `ExecutionContext` gains `tenant_id`. The worker therefore
   resolves stored credentials with the *run's* tenant (ADR 0006 §10: a
   credential id alone is never sufficient authorization).

7. **Model resolution is principal-aware (ADR 0006 §3/§9).**
   `ModelProviderFactory.resolve(ref, *, principal=None)` — the default
   `None` keeps existing callers (CLI, capabilities probe, tests) working.
   The factory holds a `CredentialResolver`; `EnvCredentialResolver`
   preserves D18's lazy env-var indirection, `StoredCredentialResolver`
   returns short-lived key material that exists only inside the
   per-resolve provider instance. `ModelRef.credential_ref` — a
   discriminated union (`env` | `stored`) — replaces `api_key_env`; a
   one-shot JSONB data migration rewrites agent-version snapshots (env
   references only; plaintext never enters a snapshot). Resolution failure
   at run time is a persisted terminal `model` failure (D5).

8. **Minimal roles, not RBAC.** Users carry `owner | admin | member`.
   Member management (create users, change roles, delete members) requires
   admin or owner; keys and credentials are manageable by any member.
   Finer-grained permissions are deferred until a second real consumer
   exists.

9. **Bootstrap is a CLI responsibility.** `jarvis tenant create`,
   `jarvis user create`, `jarvis api-key create` provision the first
   tenant/owner/key without an authenticated route (chicken-and-egg). The
   CLI remains an in-process platform consumer (D26) — it uses the
   anonymous principal and does not authenticate.

## Consequences

- `ports/` changes: new `ports/credential.py` (`CredentialResolver`,
  `ResolvedCredential`); `ModelProviderFactory.resolve` gains the
  `principal` keyword; `RunQueueMessage` gains `tenant_id` and `principal`;
  `ConversationRepo.get_or_create` gains an optional keyword-only
  `tenant_id` (the runtime stamps the conversation's owning tenant from
  `ctx.tenant_id`; `None` keeps the pre-S2 default-tenant behavior —
  additive, no existing caller breaks). One further, deliberate signature
  change: `ModelProviderFactory.resolve` and `CredentialResolver.resolve`
  become **async** — stored credentials resolve through the database
  (SqlAuthRepo), so the seam is IO and must await; env references are
  unchanged in behavior (still lazy D18 indirection, no IO). All are
  pre-declared here per CLAUDE.md rule 7 — no silent contract drift.
- Migration 0004 adds `tenants`, `users`, `sessions`, `api_keys`,
  `credentials`, and `tenant_id` columns with a default-tenant backfill;
  a follow-up migration rewrites `agent_versions.snapshot` model refs.
- The Settings UI flips on (login, members, API keys, BYOK credentials)
  and the shell drops its anonymous-mode notice — the stage's last item,
  per decision 1.6.
- New dependency: `cryptography` (AES-GCM). Password hashing is stdlib.
- A stolen database leaks no passwords (scrypt), no API keys (hashes), no
  session tokens (hashes), and no BYOK material (AES-GCM) — matching the
  security decision in ADR 0006 §Security-decision.