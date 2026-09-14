"""Workflow domain — graph validation, lints, templating (ADR 0015)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import ToolBinding
from jarvis.domain.workflow import (
    AgentNodeConfig,
    ConditionNodeConfig,
    ConditionOperator,
    ConditionRoute,
    ToolNodeConfig,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
    WorkflowVersion,
    evaluate_condition,
    lint_workflow,
    render_template,
    unreachable_nodes,
)


def _agent_node(
    node_id: str,
    agent_id: str = "agent-1",
    input_template: str = "{{input}}",
) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="agent",
        config=AgentNodeConfig(agent_id=agent_id, input_template=input_template),
    )


def _linear() -> WorkflowDefinition:
    """start → a → b, no branches."""
    return WorkflowDefinition(
        id="wf",
        name="linear",
        nodes=[
            _agent_node("start", agent_id="a1"),
            _agent_node("second", agent_id="a2", input_template="{{node.start}}"),
            _agent_node("third", agent_id="a3"),
        ],
        edges=[
            WorkflowEdge(from_node="start", to_node="second"),
            WorkflowEdge(from_node="second", to_node="third"),
        ],
        start_node_id="start",
    )


def _condition() -> WorkflowDefinition:
    """start → check -(yes)→ yes-node / -(no)→ no-node; else → no-node."""
    return WorkflowDefinition(
        id="wf",
        name="branch",
        nodes=[
            _agent_node("start", agent_id="a1"),
            WorkflowNode(
                id="check",
                type="condition",
                config=ConditionNodeConfig(
                    routes=[
                        ConditionRoute(
                            when=ConditionOperator(operator="contains", value="yes"),
                            to_node="yes-node",
                        )
                    ],
                    else_node="no-node",
                ),
            ),
            _agent_node("yes-node", agent_id="a2"),
            _agent_node("no-node", agent_id="a3"),
        ],
        edges=[
            WorkflowEdge(from_node="start", to_node="check"),
            WorkflowEdge(from_node="check", to_node="yes-node", label="yes"),
            WorkflowEdge(from_node="check", to_node="no-node", label="no"),
        ],
        start_node_id="start",
    )


def _tool_node(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        type="tool",
        config=ToolNodeConfig(
            binding=ToolBinding(
                name="calculator",
                config={"expression": "{{node.start}}"},
            )
        ),
    )


class TestGraphValidation:
    def test_valid_linear_builds(self) -> None:
        wf = _linear()
        assert wf.start_node_id == "start"
        assert len(wf.nodes) == 3
        assert wf.max_node_executions == 24  # the D44 default

    def test_valid_branch_with_condition_builds(self) -> None:
        wf = _condition()
        check = wf.nodes[1]
        assert isinstance(check.config, ConditionNodeConfig)
        assert check.config.else_node == "no-node"

    def test_tool_node_config_typed_per_type(self) -> None:
        wf = WorkflowDefinition(
            id="wf",
            name="with-tool",
            nodes=[_agent_node("start"), _tool_node("calc")],
            edges=[WorkflowEdge(from_node="start", to_node="calc")],
            start_node_id="start",
        )
        calc = wf.nodes[1]
        assert isinstance(calc.config, ToolNodeConfig)
        assert calc.config.binding.name == "calculator"

    def test_duplicate_node_id_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="dupe",
                nodes=[_agent_node("a"), _agent_node("a", agent_id="a2")],
                edges=[],
                start_node_id="a",
            )
        assert "duplicate node id 'a'" in str(excinfo.value)

    def test_dangling_edge_raises_with_field_address(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="dangling",
                nodes=[_agent_node("a")],
                edges=[WorkflowEdge(from_node="a", to_node="ghost")],
                start_node_id="a",
            )
        message = str(excinfo.value)
        assert "edges[0].to_node" in message
        assert "unknown node 'ghost'" in message

    def test_missing_start_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="no-start",
                nodes=[_agent_node("a")],
                edges=[],
                start_node_id="nowhere",
            )
        assert "start_node_id" in str(excinfo.value)

    def test_cycle_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="cyclic",
                nodes=[
                    _agent_node("a"),
                    _agent_node("b", agent_id="a2"),
                    _agent_node("c", agent_id="a3"),
                ],
                edges=[
                    WorkflowEdge(from_node="a", to_node="b"),
                    WorkflowEdge(from_node="b", to_node="c"),
                    WorkflowEdge(from_node="c", to_node="a"),
                ],
                start_node_id="a",
            )
        message = str(excinfo.value)
        assert "cycle" in message
        # every node on the cycle is named
        for node_id in ("a", "b", "c"):
            assert node_id in message

    def test_self_loop_is_a_cycle(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="self-loop",
                nodes=[_agent_node("a")],
                edges=[WorkflowEdge(from_node="a", to_node="a")],
                start_node_id="a",
            )
        assert "cycle" in str(excinfo.value)

    def test_condition_route_to_unknown_node_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="bad-route",
                nodes=[
                    _agent_node("start"),
                    WorkflowNode(
                        id="check",
                        type="condition",
                        config=ConditionNodeConfig(
                            routes=[
                                ConditionRoute(
                                    when=ConditionOperator(operator="not_empty"),
                                    to_node="nowhere",
                                )
                            ],
                            else_node="start",
                        ),
                    ),
                ],
                edges=[WorkflowEdge(from_node="start", to_node="check")],
                start_node_id="start",
            )
        message = str(excinfo.value)
        assert "routes[0].to_node" in message
        assert "unknown node 'nowhere'" in message

    def test_condition_else_to_unknown_node_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowDefinition(
                id="wf",
                name="bad-else",
                nodes=[
                    _agent_node("start"),
                    WorkflowNode(
                        id="check",
                        type="condition",
                        config=ConditionNodeConfig(
                            routes=[
                                ConditionRoute(
                                    when=ConditionOperator(operator="not_empty"),
                                    to_node="start",
                                )
                            ],
                            else_node="ghost",
                        ),
                    ),
                ],
                edges=[WorkflowEdge(from_node="start", to_node="check")],
                start_node_id="start",
            )
        assert "else_node: unknown node 'ghost'" in str(excinfo.value)

    def test_node_config_type_mismatch_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            WorkflowNode(
                id="bad",
                type="tool",
                config=AgentNodeConfig(agent_id="a1", input_template="{{input}}"),
            )
        assert "config must be a tool_config" in str(excinfo.value)

    def test_agent_node_requires_agent_id(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowNode(
                id="a",
                type="agent",
                config=AgentNodeConfig(agent_id="", input_template="{{input}}"),
            )

    def test_node_id_pattern_constrained(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowNode(
                id="a.b",  # dots would break the {{node.<id>}} template key
                type="agent",
                config=AgentNodeConfig(agent_id="a1", input_template="{{input}}"),
            )

    def test_max_node_executions_bounds(self) -> None:
        base = _linear().model_dump()
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate({**base, "max_node_executions": 0})
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate({**base, "max_node_executions": 129})
        ok = WorkflowDefinition.model_validate({**base, "max_node_executions": 128})
        assert ok.max_node_executions == 128

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowDefinition.model_validate({**_linear().model_dump(), "name": "   "})


class TestVersion:
    def test_snapshot_round_trip(self) -> None:
        version = WorkflowVersion(
            id="wv-1",
            workflow_id="wf",
            version=3,
            snapshot=_condition(),
            label="the branching one",
        )
        payload = json.loads(version.model_dump_json())
        restored = WorkflowVersion.model_validate(payload)
        assert restored == version

    def test_pin_survives_the_round_trip(self) -> None:
        node = WorkflowNode(
            id="pinned",
            type="agent",
            config=AgentNodeConfig(
                agent_id="a1",
                agent_version_id="av-7",
                input_template="{{input}}",
            ),
        )
        version = WorkflowVersion(
            id="wv-1",
            workflow_id="wf",
            version=1,
            snapshot=WorkflowDefinition(
                id="wf",
                name="pinned",
                nodes=[node],
                edges=[],
                start_node_id="pinned",
            ),
        )
        restored = WorkflowVersion.model_validate(json.loads(version.model_dump_json()))
        pinned = restored.snapshot.nodes[0].config
        assert isinstance(pinned, AgentNodeConfig)
        assert pinned.agent_version_id == "av-7"


class TestLints:
    def test_unreachable_node_is_a_warning_not_an_error(self) -> None:
        wf = _linear()
        stray = _agent_node("stray", agent_id="a9")
        wf = WorkflowDefinition(
            id=wf.id,
            name=wf.name,
            nodes=[*wf.nodes, stray],
            edges=wf.edges,
            start_node_id=wf.start_node_id,
        )
        assert unreachable_nodes(wf) == {"stray"}
        warnings = lint_workflow(wf)
        assert any("'stray'" in warning for warning in warnings)

    def test_lint_carries_stale_pins_and_memory_notes(self) -> None:
        wf = _linear()
        warnings = lint_workflow(wf, stale_pins=["start"], memory_ignored=["second"])
        assert any(
            "stale agent version" in warning and "'start'" in warning for warning in warnings
        )
        assert any("ignore" in warning and "'second'" in warning for warning in warnings)


class TestTemplating:
    def test_workflow_input_and_variables_substitute(self) -> None:
        rendered = render_template(
            "Summarize {{input}} in {{lang}}", {"input": "the news", "lang": "fr"}
        )
        assert rendered == "Summarize the news in fr"

    def test_node_output_dotted_key_substitutes(self) -> None:
        rendered = render_template(
            "Previous step said: {{node.first}}", {"node": {"first": "hello"}}
        )
        assert rendered == "Previous step said: hello"

    def test_hyphenated_node_id_substitutes(self) -> None:
        # Node ids admit `-` (_NODE_ID_PATTERN) — the template reference must
        # admit exactly the same set, or a valid id is unreferenceable and
        # the placeholder passes through untouched (live-found in the S6
        # walkthrough: {{node.agent-1}} reached the model literally).
        rendered = render_template("Fact about {{node.agent-1}}", {"node": {"agent-1": "42"}})
        assert rendered == "Fact about 42"

    def test_nested_dotted_key_reaches_dict_fields(self) -> None:
        rendered = render_template(
            "{{node.tool.json_field}}", {"node": {"tool": {"json_field": "inner"}}}
        )
        assert rendered == "inner"

    def test_unknown_var_passes_through(self) -> None:
        assert render_template("hi {{node.nobody}}", {}) == "hi {{node.nobody}}"
        assert render_template("hi {{missing}}", {}) == "hi {{missing}}"

    def test_dict_value_json_encodes(self) -> None:
        rendered = render_template("data: {{node.tool}}", {"node": {"tool": {"a": 1}}})
        assert rendered == 'data: {"a": 1}'


class TestConditions:
    def test_operator_set(self) -> None:
        assert evaluate_condition("contains", "ell", "hello") is True
        assert evaluate_condition("equals", "hello", "hello") is True
        assert evaluate_condition("equals", "hell", "hello") is False
        assert evaluate_condition("not_empty", "", "x") is True
        assert evaluate_condition("not_empty", "", "  ") is False
        assert evaluate_condition("regex", "^h.*o$", "hello") is True
        assert evaluate_condition("regex", "z", "hello") is False
