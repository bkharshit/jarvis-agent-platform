# CLAUDE.md — JARVIS project instructions

JARVIS is an open-source, production-grade AI agent platform: Python/FastAPI
backend (`src/jarvis/`), React frontend (`web/`, stage F1), built
**agent-runtime-first**. Inspired by Dify, never copying it. The Dify
checkout at `../dify-reference` (read-only) is an architectural reference
only — `docs/reference/dify-map.md` maps the verified paths.

## Authoritative docs — read before planning or changing design

- `docs/roadmap.md` — stage list (F1 + S1–S14), dependency order, per-stage
  work items, acceptance criteria, UI-enablement items
- `docs/architecture/frontend-architecture.md` — frontend IA, capability
  gating, stack, F1 commit list, anti-scope
- `docs/implementation-plan.md` — the Phase 0/1 plan (complete; historical)
- `docs/decisions.md` — philosophy 1.1–1.6 + implementation decisions D1–D24
- `docs/adr/0001..0005` — the frozen Phase 1 contracts
- `docs/architecture/overview.md` — layering and the stable-interface list

## Non-negotiable rules

1. **Dependency rule** (lint-enforced): `domain/` and `ports/` import only
   pydantic/stdlib; everything else depends inward; `mypy --strict` on
   domain/ports. Adapters are swappable by construction — keep them so.
2. **A run never raises** (D5): exactly one terminal event; the envelope
   shape is frozen (ADR 0003) — new event *types* need an ADR; failure is a
   persisted terminal state, never an exception past the runtime.
3. **Orchestrator owns limits; a strategy owns one step** (ADR 0004). The
   loop, caps, budget, deadline, cancellation, and terminal emission belong
   to `AgentRuntime` alone.
4. **Typed Pydantic-over-JSONB persistence** (ADR 0002); agent versions are
   immutable append-only snapshots (D1); every run pins a published version.
5. **Secrets are referenced, never stored** — a model credential is a
   `credential_ref` union (`env` | `stored`, ADR 0006): env refs name an
   environment *variable* (ADR 0005); stored BYOK material lives only
   AES-GCM-encrypted and is **write-only** through the API — never returned
   by GET, never logged, never snapshotted (D29/D30). Keys never appear in
   config or YAML.
6. **Frontend development model** (decision 1.6): the full product shell
   ships *alongside* the backend. Unimplemented sections render
   disabled/coming-soon, gated by `GET /v1/capabilities` — **never fake
   functionality** (no mocked data, no inert buttons). Every backend stage
   ends with a UI-enablement item; backend gates are never relaxed for UI.
7. **Contract changes need an ADR first.** Changing anything in `ports/`,
   the event envelope, or the error shape is an architecture change. New
   implementation decisions get logged in `docs/decisions.md`.

## Commands and gates

```bash
make test                  # unit suite — no DB, no network, no LLM
make lint                  # ruff check + format check
make typecheck             # mypy src
uv run pytest tests/integration -q -m db   # integration (see note)
uv run jarvis serve        # API on :8000
```

- **This machine has no Docker.** Integration tests run against local
  Postgres: role `jarvis`/`jarvis`, dev db `jarvis`, test db `jarvis_test`
  (created/migrated/truncated by `tests/integration/conftest.py`). `make
  test-db` requires Docker — do not use it here.
- One commit per plan step; gates green (`pytest tests/unit`, `ruff check
  src tests`, `mypy src`) before **every** commit.
- Frontend (once `web/` exists): `tsc --noEmit`, eslint, `vitest` green per
  commit — same discipline as the backend gates.

## Known gotchas (each cost real debugging time once)

- SQLAlchemy 2.0.52 `sa.Enum` has **no `create_type` kwarg** (silently
  ignored) — let `op.create_table` emit CREATE TYPE itself; explicit
  pre-create causes `DuplicateObjectError` (D4).
- **Cancellation is cooperative** — checked at `check_limits()`
  checkpoints; a sleeping tool finishes, the run ends at the next
  checkpoint (D6). Don't add asyncio-cancel hazards to tools.
- httpx `ASGITransport` buffers the whole response — SSE frames cannot be
  observed live mid-request in tests (workaround in the cancel test).
- The mock provider's default reply is not ReAct-parseable — mock smoke
  needs `strategy.type: function_calling` (D19); agent YAML pins its own
  `model.provider`, env vars only fill provider-less defaults.
- ReAct strips the `Final Answer:` marker from the user-facing finish
  message (D8); raw text stays in message history.
- `ToolResult.output` is **top-level** in API JSON, not `.result.output`
  (D17).
- The runtime writes a RUNNING row at run start; stream resume prefers
  the live sink before the DB row (D2/D3).
- **Model resolution is inside the runtime's try block (D28)** — a
  credential resolution failure must become a persisted terminal `model`
  failure; an escape past the runtime makes the worker treat the message as
  a claim failure and retry it forever with no terminal state.
- **JSONB data migrations match on VALUE, never key presence** — the
  pre-S2 serializer wrote explicit nulls (`api_key_env: null`), and `?`
  matches a JSON null while `->>` reads it back as SQL NULL. Migration
  0005's key-presence WHERE rewrote nulls into unparseable
  `credential_ref` blobs and 500'd every affected listing (found live in
  the walkthrough; regression-tested in test_migrations).
- **The resume route attaches beyond the old pause — its row status must
  not end the stream** (`end_on_pause_status=False` on the resume path
  only, S10 pause-again race): when a resumed segment pauses again, the
  run row still shows the OLD `awaiting_input`, and reading that as a
  stream end made the blocking resume 500 with "never reached a segment
  end" (found live; regression-tested in test_api_run.py).
- **CSV env fields need `Annotated[list[str], NoDecode]` + a
  mode="before" validator** (S3, D35) — pydantic-settings treats a plain
  `list[str]` env value as JSON and explodes on `a,b`. Same for any
  complex-typed env field.
- **The CLI is its own process** (S3, found live): an allow-list inlined
  on the `serve` command line leaves the `jarvis` CLI process without it
  (`agent create` rejects plugin strategies). Export
  `JARVIS_STRATEGY_PLUGIN_ALLOWLIST` for the whole shell session.
- **Strategy phases advance by transcript position (assistant-turn
  count), never marker detection** (S3, found live): models drift on
  marker placement ("…DONE:" at the end), casing, and wording
  ("✅ B:" for "PICKED:"). Markers stay in instructions + case-insensitive
  substring done checks; the turn count decides the phase.
- **Plugins must stream `text.delta` via `client.stream()` like
  function_calling does** (S3, found live) — a plugin that only
  `generate()`s produces runs whose console iterations render empty.
- **A plugin must distinguish the two `TextDelta` classes** (S3, found
  live): `jarvis.models.types.TextDelta` is the stream delta
  `client.stream()` yields; `jarvis.domain.events.TextDelta` is the sink
  event. An isinstance check against the *event* class silently drops
  every delta — empty console iteration AND an empty persisted assistant
  message, while the run still "succeeds". Import with `as
  ModelTextDelta` (see the fixture invoke helper).
- **Tests are hermetic: `Settings(_env_file=None)`** (S3, found live) —
  the developer's gitignored `./.env` (secrets, allow-lists, run limits)
  leaks into bare `Settings()` and silently changes expectations.
- **There are two `max_iterations` caps** (ADR 0004): the platform
  `RunLimits` (`JARVIS_RUN_MAX_ITERATIONS`, enforced at loop top) and the
  per-agent value from the definition snapshot (loop bottom). Per-agent can
  only be *tighter* — a UI editor allowing 32 cannot raise the platform
  ceiling, and the failure message does not yet say which cap fired.
- **A paused run returns to the worker BEFORE the claim is acked — acks
  must be conditional** (S4, found live; pre-existing S10 race):
  `SqlRunQueue.ack` succeeds only while the queue row is still
  `status == "claimed"`, or a fast client's `enqueue_resume` (row →
  pending) is clobbered back to done by the late ack and the resume is
  never claimed — the blocking resume route then hangs forever
  (regression-tested in test_resume_flow.py). Debug lesson:
  `Task.print_stack` shows only the OUTERMOST coroutine frame — walk
  `coro.cr_await` recursively to find the blocked await.
- **Capabilities are derived facts read from the DB** (S4, found live):
  the MCP panel flips on a listing of `mcp_servers` rows, so until the
  registry migration is applied every `GET /v1/capabilities` 500s — and
  the integration suite self-migrates, so it can never see this class
  of bug.
- **anyio TaskGroup wraps the real MCP handshake error in an
  ExceptionGroup** (S4, found live): unwrap to the root cause
  (`_root_cause` in tools/mcp/connection.py) or the probe's 502 names
  the group, not the failure. Tavily's API wants the header value RAW —
  `Authorization: <key>`, no `Bearer` prefix (verified by a curl matrix).
- **The credentials master key lives ONLY in the process env — `.env`'s
  value must be the base64 key itself, never a var-name indirection**
  (S6, found live; cost three stored credentials): `.env` carried
  `JARVIS_CREDENTIALS_MASTER_KEY=JARVIS_MASTER_KEY_VALUE` (a variable
  *name*), while the real key existed only as an export in a long-lived
  shell. `set -a; source .env` before a serve clobbered the live value
  with the placeholder and nothing could decrypt again — rotation
  (delete + re-store) was the only path. Diagnosis: every stored
  envelope carries a `key_id` fingerprint (sha256 of the decoded key,
  first 16 hex) — verify ANY candidate key against it before
  installing; an unmatched key fails with InvalidTag, and a missing/
  malformed one with "must decode to exactly 32 bytes". Loss fails
  loudly by design (ADR 0006/D30): there is no silent fallback, and
  KMS/re-wrap is the deferred future path.
- **Workflow template references must admit exactly what the node-id
  pattern admits** (S6, found live): the template regex admitted
  `[\w.]` while node ids allow hyphens, so `{{node.agent-1}}` never
  matched and passed through to the model literally — only bare-word
  ids in every test and fixture masked it. Fixed + regression-tested
  (test_hyphenated_node_id_substitutes); the invariant to keep: widen
  or narrow the two patterns together.

## Working agreement

- Stages build autonomously, commit by commit. **Working agreement
  (revised 2026-09-08)**: no per-stage deep design walkthroughs. Per
  stage the ritual is ONE manual-testing session together, live in the
  UI/API with the stage's `docs/walkthrough-<stage>.md` as the script,
  then the docs-closure commit. Product-wide walkthroughs only on
  explicit request.
- Each new stage gets its own implementation-plan-style doc (commit
  sequence, gates, tests) at build time, the way Phase 1 did.
- Prefer the existing seam over a new abstraction: every roadmap stage
  rides on a `ports/` Protocol that already exists.

## Skills

Workflow skills live in `.claude/skills/` (adapted from Dify's structure,
specialized to this stack):

- `backend-code-review` — explicit backend review requests (`src/jarvis/`,
  `tests/`, migrations)
- `frontend-code-review` — explicit frontend review requests (`web/`)
- `frontend-testing` — writing/changing vitest + RTL tests under `web/`
- `how-to-write-component` — component ownership/state/data decisions in
  `web/`

Not installed: Dify's `e2e-cucumber-playwright` (we don't use Cucumber; add
a Playwright skill when the e2e suite exists).