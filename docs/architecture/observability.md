# Observability (future phase — stub)

Anticipated seam: `trace_id` is already plumbed through `ExecutionContext`,
persisted on executions, and emitted in `run.started` — OpenTelemetry wiring
attaches there without touching domain code. The durable event log doubles as
run-level observability until then. Not started.