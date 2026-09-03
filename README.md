# JARVIS

An open-source, production-grade AI agent platform. Agent runtime first —
tools, MCP, workflows, RAG, and frontend ride on the interfaces the runtime
establishes.

> Status: Phase 1 (agent runtime) under construction. See
> `docs/implementation-plan.md`.

## Quickstart

```bash
uv sync --extra dev
docker compose up -d postgres
uv run alembic upgrade head
uv run jarvis serve          # API on :8000
```

CLI:

```bash
uv run jarvis agent create --file examples/research-agent.yaml
uv run jarvis run research-agent "Explain Kafka consumer groups" --stream
uv run jarvis executions list
```