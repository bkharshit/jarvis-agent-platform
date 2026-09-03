# ADR 0002 — Typed Pydantic-over-JSONB persistence

- **Status**: Accepted
- **Date**: 2026-09-04

## Context

Dify persists large swaths of state as LongText columns holding JSON strings
(`api/models/workflow.py`, `api/models/agent.py`). That is untyped,
unindexable, and drifts silently from the Python models.

## Decision

Every persisted payload is a **Pydantic model serialized to a JSONB column**:
agent version snapshots, message content, tool arguments/results, event
payloads, usage. Scalar query/filter fields (status, timestamps, ids,
sequences) remain real SQL columns. The Pydantic model is the single schema
authority; SQLAlchemy rows hold `payload: dict` fields validated through the
domain models on write and read.

## Consequences

- Schema drift is impossible without a validation failure.
- Payloads are queryable/indexable (GIN) when needed.
- `extra="forbid"` domain models reject unknown persisted fields loudly.
- Cost: a serialization boundary at the repository edge — that is where it
  belongs (see ports/repository.py).