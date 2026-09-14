"""Workflow definitions, immutable version snapshots, and graph validation.

A workflow is a DAG of agent/tool/condition nodes executed by a dedicated
sibling runtime (ADR 0015). The domain layer is pure Pydantic — no IO: graph
validation runs at the model boundary (create/update → 422), acyclicity via
topological check, and every node's config is typed per node type. Agent
nodes pin `agent_version_id` at PUBLISH time (D42) — the snapshot freezes
it, so a workflow version's meaning never changes under it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.domain.agent import ToolBinding

_NODE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
# The workflow template pattern is the prompt engine's `{{var}}` with one
# extension: `node.<id>` names an upstream node's output (ADR 0015 §2).
_TEMPLATE_PATTERN = re.compile(r"\{\{([\w.]+)\}\}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentNodeConfig(_Model):
    """Runs one agent with a `{{...}}`-templated input. `agent_version_id`
    is None on drafts and pinned by `update_and_publish` (D42). Template
    variables: the workflow `input`, upstream outputs `node.<id>`, and run
    `variables`."""

    agent_id: str = Field(min_length=1)
    agent_version_id: str | None = None
    input_template: str = Field(min_length=1)


class ToolNodeConfig(_Model):
    """Executes one bound tool (builtin or `mcp__server__tool`).
    `arguments` are the tool call's arguments — string values may carry
    `{{...}}` templates; `binding.config` stays the tool-level config
    (timeout etc.), exactly the ToolContext.config an agent run passes."""

    binding: ToolBinding
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConditionOperator(_Model):
    """A closed operator set (ADR 0015 §2) — no model call, no Jinja."""

    operator: Literal["contains", "equals", "regex", "not_empty"]
    value: str = ""


class ConditionRoute(_Model):
    """First matching route wins; the node's config carries the else."""

    when: ConditionOperator
    to_node: str


class ConditionNodeConfig(_Model):
    """Evaluated against the most recent upstream node's output text (the
    single predecessor edge taken — sequential walks make this
    unambiguous). `else_node` is required."""

    routes: list[ConditionRoute] = Field(min_length=1)
    else_node: str = Field(min_length=1)


class _Missing:
    """Sentinel — an unknown template var (None is a legitimate value)."""


_MISSING = _Missing()

_CONFIG_BY_TYPE: dict[str, type] = {
    "agent": AgentNodeConfig,
    "tool": ToolNodeConfig,
    "condition": ConditionNodeConfig,
}


class WorkflowNode(_Model):
    id: str = Field(min_length=1, pattern=_NODE_ID_PATTERN.pattern)
    type: Literal["agent", "tool", "condition"]
    config: AgentNodeConfig | ToolNodeConfig | ConditionNodeConfig

    @model_validator(mode="after")
    def _config_matches_type(self) -> WorkflowNode:
        expected = _CONFIG_BY_TYPE[self.type]
        if not isinstance(self.config, expected):
            raise ValueError(
                f"node {self.id!r} ({self.type}) config must be a "
                f"{self.type}_config, got {type(self.config).__name__}"
            )
        return self


class WorkflowEdge(_Model):
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    label: str | None = None  # the condition-route name (canvas display)


class WorkflowDefinition(_Model):
    id: str
    name: str = Field(min_length=1)
    description: str = ""
    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    start_node_id: str = Field(min_length=1)
    # The orchestrator-owned cap (D44, ADR 0004 transposed): the executor
    # owns the limit, a node owns one step.
    max_node_executions: int = Field(default=24, ge=1, le=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow name must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        problems = validate_graph(
            [node.id for node in self.nodes],
            self.edges,
            self.start_node_id,
        )
        if problems:
            raise ValueError("; ".join(problems))
        for node in self.nodes:
            problems = validate_node_references(node, {n.id for n in self.nodes})
            if problems:
                raise ValueError("; ".join(problems))
        return self


class WorkflowVersion(_Model):
    """Immutable, append-only snapshot of a WorkflowDefinition — the same
    D1 replay argument as AgentVersion, one level up."""

    id: str
    workflow_id: str
    version: int = Field(ge=1)
    snapshot: WorkflowDefinition
    label: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- graph validation ---------------------------------------------------------


def validate_graph(
    node_ids: list[str],
    edges: list[WorkflowEdge],
    start_node_id: str,
) -> list[str]:
    """Structural graph problems as field-addressable messages (the API maps
    them to 422). Unreachability is NOT here — drafts may keep stray nodes;
    `lint_workflow` names them as warnings instead."""
    problems: list[str] = []
    seen: set[str] = set()
    for node_id in node_ids:
        if node_id in seen:
            problems.append(f"nodes: duplicate node id {node_id!r}")
        seen.add(node_id)
    for index, edge in enumerate(edges):
        if edge.from_node not in seen:
            problems.append(f"edges[{index}].from_node: unknown node {edge.from_node!r}")
        if edge.to_node not in seen:
            problems.append(f"edges[{index}].to_node: unknown node {edge.to_node!r}")
    if start_node_id not in seen:
        problems.append(f"start_node_id: {start_node_id!r} is not a node in the graph")
    if problems:
        return problems
    problems.extend(validate_acyclic(node_ids, edges))
    return problems


def validate_acyclic(node_ids: list[str], edges: list[WorkflowEdge]) -> list[str]:
    """Kahn's topological check — a cycle lists the nodes still stuck in it."""
    remaining_edges = [(edge.from_node, edge.to_node) for edge in edges]
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for src, dst in remaining_edges:
        indegree[dst] += 1
        outgoing[src].append(dst)
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node_id = ready.pop()
        visited += 1
        for nxt in outgoing[node_id]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if visited != len(node_ids):
        stuck = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        return [f"nodes: the graph has a cycle through {', '.join(stuck)}"]
    return []


def validate_node_references(node: WorkflowNode, known_ids: set[str]) -> list[str]:
    """Condition nodes route to in-graph nodes (routes + else). Agent and
    tool node configs carry no in-graph references."""
    if node.type != "condition" or not isinstance(node.config, ConditionNodeConfig):
        return []
    problems: list[str] = []
    config = node.config
    for index, route in enumerate(config.routes):
        if route.to_node not in known_ids:
            problems.append(
                f"nodes[{node.id!r}].routes[{index}].to_node: unknown node {route.to_node!r}"
            )
    if config.else_node not in known_ids:
        problems.append(f"nodes[{node.id!r}].else_node: unknown node {config.else_node!r}")
    return problems


def lint_workflow(
    definition: WorkflowDefinition,
    *,
    stale_pins: list[str] | None = None,
    memory_ignored: list[str] | None = None,
) -> list[str]:
    """Non-fatal warnings the API returns alongside the definition (ADR 0015
    §1): unreachable nodes in drafts, stale agent pins (a newer version
    exists), memory-enabled node agents (node agents run memory-less in v1).
    Lints are never failures."""
    warnings = [
        f"node {node_id!r} is unreachable from the start node"
        for node_id in unreachable_nodes(definition)
    ]
    warnings.extend(f"node {node_id!r} pins a stale agent version" for node_id in stale_pins or [])
    warnings.extend(
        f"node {node_id!r} enables memory, which node agents ignore"
        for node_id in memory_ignored or []
    )
    return warnings


def unreachable_nodes(definition: WorkflowDefinition) -> set[str]:
    """Nodes with no path from the start (edges only — condition routes are
    runtime choices, not graph structure, and never expand reachability)."""
    known = {node.id for node in definition.nodes}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in known}
    for edge in definition.edges:
        outgoing[edge.from_node].append(edge.to_node)
    reached = {definition.start_node_id}
    frontier = [definition.start_node_id]
    while frontier:
        for nxt in outgoing[frontier.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return known - reached


# --- templating -----------------------------------------------------------------


def render_template(template: str, values: dict[str, Any]) -> str:
    """`{{...}}` substitution for node inputs and tool arguments — the
    prompt engine's plain substitution with one extension: `node.<id>`
    names an upstream node's output. Unknown vars pass through untouched,
    exactly like the prompt engine. A dotted key resolves against a flat
    `node.<id>` entry first, then walks nested dicts
    (`node.<id>.<field>`)."""

    def _resolve(key: str) -> Any:
        if key in values:
            return values[key]
        value: Any = values
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return _MISSING
        return value

    def _substitute(match: re.Match[str]) -> str:
        value = _resolve(match.group(1))
        if value is _MISSING:
            return match.group(0)  # unknown vars pass through untouched
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return _TEMPLATE_PATTERN.sub(_substitute, template)


def evaluate_condition(operator: str, value: str, text: str) -> bool:
    """A condition node's test against the upstream output text (ADR 0015
    §2). `regex` uses `re.search` (a non-match pattern routes to else)."""
    if operator == "not_empty":
        return bool(text.strip())
    if operator == "equals":
        return text == value
    if operator == "contains":
        return value in text
    if operator == "regex":
        return re.search(value, text) is not None
    raise ValueError(f"unknown condition operator {operator!r}")


__all__ = [
    "AgentNodeConfig",
    "ConditionNodeConfig",
    "ConditionOperator",
    "ConditionRoute",
    "ToolNodeConfig",
    "WorkflowDefinition",
    "WorkflowEdge",
    "WorkflowNode",
    "WorkflowVersion",
    "evaluate_condition",
    "lint_workflow",
    "render_template",
    "unreachable_nodes",
    "validate_acyclic",
    "validate_graph",
    "validate_node_references",
]
