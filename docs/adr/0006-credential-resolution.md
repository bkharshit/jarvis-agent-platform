# ADR 0006 — Credential Resolution: References with Optional Encrypted Storage

- **Status**: Accepted (decision only — implementation deferred to stage S2/BYOK)
- **Date**: 2026-09-05
- **Amends**: D18 ("Secrets are referenced, never stored") — see `docs/decisions.md` §3

## Context

Today an agent's `ModelRef` carries `api_key_env` — the *name* of an
environment variable holding the provider API key (D18, ADR 0005). The
secret itself never enters the domain, the database, or a version
snapshot; `OpenAICompatibleProvider._headers()` resolves it from the
process environment lazily at request time. This is the correct design
for self-hosted deployments and stays exactly as it is.

It cannot express multi-tenant BYOK: a hosted deployment cannot put every
user's OpenAI/Gemini/Anthropic key into the backend environment, and an
environment variable has no owner, no UI, no rotation, and no revocation.
A full architecture review (2026-09-05) concluded that the right response
is to **make the architectural decision now** — so the current design is
not evolved incompatibly in the meantime — and **implement it as part of
S2** (auth & multi-tenancy), because a stored credential without a tenant
ownership model has no meaningful owner.

## Decision

1. **Agent definitions and immutable agent-version snapshots never contain
   plaintext credentials.** A snapshot may contain a credential
   *reference* (an environment-variable name or a credential id), never
   credential material. This preserves the load-bearing part of D18 for
   everything an agent touches.

2. **Agents reference credentials rather than containing credential
   material.** The reference is the only credential-related thing that is
   persisted, versioned, or returned by APIs.

3. **Credential materialization is owned by a dedicated
   `CredentialResolver` abstraction** — a new `ports/` Protocol at S2
   implementation time. No other layer reads environment variables or a
   credential store to obtain a key.

4. **The resolver may have multiple implementations:**
   - an environment-variable resolver (self-hosted), and
   - a stored/encrypted credential resolver (hosted multi-tenant/BYOK).
   Both implement the same Protocol; provider and runtime code do not
   fork.

5. **Environment variables remain the default and simplest mechanism for
   self-hosted deployments.** A self-hosted user never needs a KMS, a
   credentials table, or anything beyond `OPENAI_API_KEY=...` unless they
   opt in.

6. **Hosted multi-tenant deployments may support user-supplied BYOK
   credentials stored encrypted at rest**, addressed by credential id and
   owned by a tenant.

7. **Credentials are write-only through the API:**
   - plaintext may *enter* through credential creation/update endpoints;
   - plaintext is **never** returned by GET APIs (response schemas have
     no field for it — leakage must be a schema violation, not a
     discipline);
   - plaintext never appears in agent snapshots;
   - plaintext is never logged (including error messages, event
     payloads, and validation errors — the current 422 handler already
     omits pydantic `input`).

8. **Tenant ownership and authorization are enforced at the credential
   repository/resolution boundary** — the same repo-scoping pattern as the
   rest of S2 (WHERE clauses, not post-filters; 404, not 403).

9. **The runtime/provider layer receives already-resolved credential
   material** and does not know whether it came from an environment
   variable, the database, or a secrets manager. `OpenAICompatibleProvider`
   keeps taking an `api_key`; the resolver is injected behind
   `ModelProviderFactory`, the single seam between "what an agent
   references" and "how the key materializes".

10. **The exact encryption/KMS implementation is intentionally deferred to
    S2 implementation** and is not specified by this ADR (see
    *Deliberately deferred* below). The security properties required of it
    *are* decided (see *Security decision*).

## The current system is unchanged until S2

`api_key_env` is **not** migrated to a `credential_ref` union now. Doing so
provides no user-facing capability: the stored-credential resolver does not
exist yet, and neither does the tenant ownership model that makes a stored
credential mean anything. Adding an optional field to `ModelRef` later is
backward-compatible with every stored JSONB snapshot (`extra="forbid"`
makes stale writers fail loudly, but old snapshots simply lack the field),
so deferral carries no retrofit cost.

## Target architecture (conceptual)

```
Agent
  │
  │ credential reference
  ▼
ModelProviderFactory
  │
  ▼
CredentialResolver
  ├── EnvCredentialResolver
  │      └── process environment
  │
  └── StoredCredentialResolver
         └── credential repository
                └── encrypted secret
                       └── KMS / secure key
```

Exact class names, table columns, and payload shapes are S2 implementation
details. One ports consequence is decided now, though: stored credential
resolution is tenant-scoped, and `ModelProviderFactory.resolve(ref)` carries
no principal — so S2 must thread tenant/principal context through the
resolve path (a `ports/` contract change, covered by this ADR, not a later
surprise).

## Security decision

What encryption at rest **does** protect: compromise of data *outside* the
running application's trust boundary — the database at rest, database
backups, and ciphertext-only appearances (e.g. in leaked logs). For a
stored-credential system this is a hard requirement, not an enhancement:
D18's env-reference model already guarantees a stolen DB/backup contains no
secrets, and storage must not regress that.

What encryption at rest does **NOT** do:

- it does **not** protect against backend RCE/server compromise — the app
  must decrypt in-process to make LLM calls, so an attacker with app access
  obtains plaintext regardless of the crypto;
- it does **not** replace tenant authorization — encryption adds nothing
  to tenant isolation, which lives in repo/resolver scoping;
- it does **not** solve frontend/XSS compromise — the user's key passes
  through the browser exactly once at entry; a compromised browser can
  capture it there in any design;
- it does **not** replace logging/redaction controls — plaintext flows
  through resolver code, so log hygiene is a *new* risk to be controlled,
  not one encryption mitigates.

Encryption does not make the application secure against server compromise,
and this decision does not claim it does.

Security priorities, in order:

1. tenant isolation;
2. no secret in API responses;
3. no secret in snapshots;
4. no secret in logs, errors, or events;
5. correct credential authorization (tenant-scoped resolution);
6. encryption at rest;
7. KMS/separation of encryption keys.

Items 1–5 are the load-bearing controls; 6–7 protect against backup/DB
leaks and limit the key-compromise blast radius.

## Self-hosted vs SaaS

Self-hosted (default — unchanged):

```
OPENAI_API_KEY=...
Agent → environment reference → resolver → env
```

Hosted SaaS:

```
User → credential UI → encrypted credential store
Agent → credential reference → resolver → credential store
```

The same provider/runtime code serves both; the deployment chooses which
resolvers are *available* (self-hosted: env only by default, stored
opt-in; SaaS: stored on, KEK in a managed KMS). The credentials UI is
capability-gated (`GET /v1/capabilities`) per decision 1.6 — never faked.

## S2 migration

When S2/BYOK begins, the intended migration is:

```
api_key_env
    ↓  (one-shot JSONB data migration over agent version snapshots)
credential_ref
    ↓
EnvCredentialRef | StoredCredentialRef
```

The migration must preserve existing agent-version semantics (snapshots
stay otherwise byte-identical, ADR 0002 replay unaffected) and must never
introduce plaintext keys into snapshots. Credential failures at run time —
missing, revoked, or wrong-tenant reference — surface as persisted
terminal `model` failures (D5: a run never raises); the frozen event
envelope and `ErrorKind` literal are untouched.

## Tenant context invariant

Stored credential resolution is tenant-scoped. **A credential ID alone is
never sufficient authorization.** Conceptually,

```
resolve(tenant/principal context, credential_ref)
```

must verify that the caller is allowed to use the credential; a valid
credential id owned by another tenant resolves to "not found", not to
that tenant's key. This is a security invariant, not an implementation
nicety.

## Deliberately deferred to S2 implementation (not decided today)

- exact credentials table schema
- AES-GCM implementation details
- DEK/KEK structure (envelope-encryption shape)
- KMS vendor choice (managed KMS for SaaS; local master-key env var for
  self-hosted opt-in)
- key rotation implementation (KEK re-wrap vs credential re-encrypt)
- credential validation endpoint/behavior
- credential usage audit events
- crypto-erasure / per-tenant KMS keys (needed only for contractual
  provable-deletion; explicitly not an early requirement)
- credential billing/metering

## Consequences

- No application code, domain models, persistence schemas, migrations, or
  frontend change now. `api_key_env` remains the only mechanism.
- D18 is amended with a pointer to this ADR; its rule remains literally
  true today and, even post-S2, remains true for everything an agent
  *persists* (reference, never material).
- `CLAUDE.md` rule 5 is updated at S2 implementation time, when the
  behavior it describes actually changes.
- S2 gains a BYOK work item (see `docs/roadmap.md` S2) covering the
  resolver port, stored credentials, tenant scoping, and the API/UI —
  in that stage, not before.
- The review's implementation notes (seams, one-table platform+BYOK
  ownership, soft-revoke tombstones, per-run plaintext lifetime) are
  suggestions to the S2 implementer, not commitments of this ADR.