"""MCP adapter internals (S4, ADR 0012 §6) — the ONLY module that touches
the mcp SDK. The runtime, API, and tests never import it directly; unit
tests fake the connection seam, so the SDK can be swapped without touching
the runtime (adapters swappable by construction).

S14 (ADR 0018 §1): eager toolset resolution failures share a base —
`ToolResolutionError` — so the three runtime catch sites (AgentRuntime.
run/resume, WorkflowRuntime.run) catch the base and one new provider adds
its own subclass without touching them again."""

from __future__ import annotations


class ToolResolutionError(Exception):
    """A bound tool family failed its eager per-segment resolution (D38,
    fifth application): the run's persisted terminal is run.failed with
    error_kind="tool", the resource named — never an escape past the
    runtime (the worker would treat it as a claim failure and retry)."""


class McpResolutionError(ToolResolutionError):
    """A bound MCP server is missing, disabled, unreachable, or its auth
    env var is absent. Becomes exactly one persisted terminal `run.failed`
    with error_kind="tool" naming the server."""

    def __init__(self, server_name: str, detail: str) -> None:
        super().__init__(f"MCP server {server_name!r}: {detail}")
        self.server_name = server_name


class SubAgentResolutionError(ToolResolutionError):
    """A bound sub-agent is missing, invisible to the tenant, unpublished,
    or its binding carries no agent_id (S14, ADR 0018 §1 — D29: a foreign
    sub-agent reads as absent). Becomes exactly one persisted terminal
    `run.failed` with error_kind="tool" naming the agent."""

    def __init__(self, agent_ref: str, detail: str) -> None:
        super().__init__(f"sub-agent {agent_ref!r}: {detail}")
        self.agent_ref = agent_ref


class RetrieverResolutionError(ToolResolutionError):
    """A bound `retriever__*` dataset is missing or invisible to the
    tenant (S8, ADR 0020 §5 — D29: a foreign dataset reads as absent), or
    knowledge retrieval itself failed (D66: the same terminal `tool`
    kind, no new catch site)."""

    def __init__(self, dataset_ref: str, detail: str) -> None:
        super().__init__(f"retriever {dataset_ref!r}: {detail}")
        self.dataset_ref = dataset_ref


__all__ = [
    "McpResolutionError",
    "RetrieverResolutionError",
    "SubAgentResolutionError",
    "ToolResolutionError",
]
