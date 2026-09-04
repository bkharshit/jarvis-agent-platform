# Dependency Rule

Authority: `CLAUDE.md` rule 1, `docs/architecture/overview.md`,
`docs/decisions.md` 1.3.

## The contract

- `domain/` and `ports/` import **only** pydantic and the stdlib. Anything
  else in those packages is a violation — including asyncio, SQLAlchemy,
  httpx, FastAPI, and other JARVIS packages (domain/ports must not import
  each other's adapters, and ports never imports runtime).
- Everything else depends inward: `api/`/`cli/` → runtime → ports/domain.
  Delivery layers must contain no logic — if a route or command grows a
  branch that decides behavior, that branch belongs in the runtime or a
  repository.
- `mypy --strict` passes on `domain/` and `ports/`.
- CLI and HTTP share `AppContainer`; neither re-implements what the other
  has.

## What to flag

- A new import in `domain/` or `ports/` that is not pydantic/stdlib.
- Business logic in `api/routes/` or `cli/` (transport must be thin:
  parse → delegate → serialize).
- A Protocol gaining a default implementation or an import of an adapter
  (ports define seams; adapters live elsewhere).
- Dual implementations of the same behavior (CLI re-doing runtime logic).
- Circumvention via `Any`, `# type: ignore`, or untyped defs inside
  domain/ports to sneak a dependency through.

## Severity calibration

- New non-pydantic import in domain/ports: **P1** (breaks the lint-enforced
  rule and the swappability guarantee).
- Behavior in a transport layer: **P2** unless it duplicates runtime logic
  with drift risk, then **P1**.