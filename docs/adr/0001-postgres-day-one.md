# ADR 0001 — PostgreSQL from day one

- **Status**: Accepted
- **Date**: 2026-09-04

## Context

JARVIS persists agents, immutable version snapshots, executions, messages,
tool executions, and a durable event log (Phase 1 scope). Dify started on
SQLite-friendly patterns and paid a migration tax; it also leans on Redis for
streams/state that we consider durable run history.

## Decision

PostgreSQL from day one, via docker compose for dev and integration tests.
No SQLite path, no MongoDB. Pure-domain unit tests never touch the DB; only
`-m db` integration tests do.

## Consequences

- Dev requires `docker compose up -d postgres` (or a local Postgres) — accepted.
- JSONB gives us typed, queryable event/payload persistence (see ADR 0002).
- The durable event log (cursor PK) is authoritative for SSE replay, so no
  Redis dependency in Phase 1.
- Alembic migrations from the first commit; up/down tested in CI.