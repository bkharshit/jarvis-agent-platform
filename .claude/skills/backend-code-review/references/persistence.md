# Persistence

Authority: ADR 0001/0002, `docs/decisions.md` D1–D4, D22,
`docs/architecture/data-model.md`, `CLAUDE.md` rule 4.

## The contract

- **Typed Pydantic-over-JSONB**: every persisted payload is a Pydantic
  model serialized into a JSONB column (`extra="forbid"`); scalars stay
  real columns; the domain model is the single schema authority.
  Serialization happens only at the repository edge.
- **Agent versions are immutable append-only snapshots** (D1):
  `update_and_publish` appends; nothing rewrites a published snapshot;
  every run pins a `agent_version_id`. `DELETE /agents/{id}` returns 409
  when executions exist — history is never orphaned.
- **The runtime writes a RUNNING row at run start** (D2) so live runs are
  visible and cancellable.
- Per-run `messages` and `execution_events` sequences are unique-constrained;
  the global `cursor` is monotonic.
- Migrations: up and down both tested; **`op.create_table` emits CREATE
  TYPE itself — never pre-create a PG enum explicitly** (SQLAlchemy 2.0.52
  ignores `create_type`; D4). Downgrades keep explicit
  `.drop(checkfirst=True)`.
- Integration tests self-bootstrap a dedicated `jarvis_test` database
  (created, migrated up/down/up, truncated per test) — tests must not
  touch the dev database or assume Docker.

## What to flag

- Raw JSON strings or hand-rolled `json.loads` payloads in repos; Pydantic
  models with `extra="allow"` persisted as-is; schema drift that would
  fail loudly being softened with `extra="ignore"`.
- Any update to an existing `agent_versions.snapshot` row; deletes that
  orphan executions.
- A migration without a downgrade, an explicit enum pre-create, or enum
  alteration without remembering D4.
- Repo queries leaking SQL into route/CLI code; transactions spanning
  outside a repository boundary; session usage across event-loop tasks.
- New tables missing the JSONB/Pydantic treatment or without an
  accompanying domain model as authority.

## Severity calibration

- Snapshot mutation or history loss: **P0**. Payload schema drift /
  untyped columns: **P1**. Migration that cannot roll back: **P2** (P1 if
  it would break `alembic upgrade` on a fresh DB).