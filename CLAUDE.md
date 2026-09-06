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

## Working agreement

- Stages build autonomously, commit by commit. But **before starting a new
  stage**: give a deep, layer-by-layer walkthrough of what was implemented
  and why (tied to plan sections and ADRs), and confirm before proceeding.
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