"""SqlWorkflowRepo integration (S6, ADR 0015): the agents repo pair mirrored
one level up — append-only versions, tenant scoping (shared NULL rows
visible everywhere, foreign ids resolve to None/False), delete refused
while executions exist (workflow runs are agent_executions rows, D41)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from jarvis.domain.workflow import (
    AgentNodeConfig,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)

pytestmark = pytest.mark.db

TENANT = "default"  # seeded by migration 0004 / conftest truncate
OTHER_TENANT = "tenant-b"


def _workflow(name: str = "fixtures-wf", pin: str | None = None) -> WorkflowDefinition:
    node = WorkflowNode(
        id="only",
        type="agent",
        config=AgentNodeConfig(
            agent_id="agent-1", agent_version_id=pin, input_template="{{input}}"
        ),
    )
    return WorkflowDefinition(id=str(uuid4()), name=name, nodes=[node], start_node_id="only")


async def test_create_and_get_roundtrip(container) -> None:
    created = await container.workflows.create(_workflow())
    assert created.name == "fixtures-wf"

    loaded = await container.workflows.get(created.id, tenant_id=TENANT)
    assert loaded == created
    version = await container.workflows.latest_version(created.id, tenant_id=TENANT)
    assert version is not None and version.version == 1
    assert version.snapshot == created
    assert version.snapshot.nodes[0].config.agent_id == "agent-1"  # type: ignore[union-attr]


async def test_update_and_publish_is_append_only(container) -> None:
    wf = await container.workflows.create(_workflow())
    changed = wf.model_copy(update={"description": "changed"})
    version = await container.workflows.update_and_publish(changed)
    assert version.version == 2

    v1 = await container.workflows.get_version(wf.id, 1, tenant_id=TENANT)
    assert v1 is not None and v1.snapshot.description == ""  # history untouched
    versions = await container.workflows.list_versions(wf.id, tenant_id=TENANT)
    assert [v.version for v in versions] == [1, 2]
    latest = await container.workflows.latest_version(wf.id, tenant_id=TENANT)
    assert latest is not None and latest.snapshot.description == "changed"


async def test_get_version_by_id_resolves_the_worker_loader(container) -> None:
    wf = await container.workflows.create(_workflow(pin="av-pinned"))
    version = await container.workflows.latest_version(wf.id, tenant_id=TENANT)
    assert version is not None
    loaded = await container.workflows.get_version_by_id(version.id)
    assert loaded == version
    assert await container.workflows.get_version_by_id("no-such-version") is None


async def test_duplicate_name_conflicts_at_the_db_boundary(container) -> None:
    wf = _workflow("dup-wf")
    await container.workflows.create(wf)
    with pytest.raises(IntegrityError):
        await container.workflows.create(_workflow("dup-wf"))


async def test_shared_rows_visible_to_every_tenant(container) -> None:
    wf = await container.workflows.create(_workflow("shared-wf"))
    loaded = await container.workflows.get(wf.id, tenant_id=OTHER_TENANT)
    assert loaded == wf  # NULL tenant = platform-shared


async def test_owned_and_shared_names_coexist(container) -> None:
    """The 0007 pattern: one shared row and one owned row with the same
    name are distinct rows (partial unique indexes)."""
    await container.workflows.create(_workflow("dual-name"))  # shared
    owned = await container.workflows.create(_workflow("dual-name"), tenant_id=TENANT)
    assert owned is not None
    by_name = await container.workflows.get_by_name("dual-name", tenant_id=TENANT)
    assert by_name is not None and by_name == owned


async def test_foreign_tenant_gets_none_and_delete_false(container) -> None:
    wf = await container.workflows.create(_workflow("owned-wf"), tenant_id=TENANT)
    assert await container.workflows.get(wf.id, tenant_id=OTHER_TENANT) is None
    assert await container.workflows.delete(wf.id, tenant_id=OTHER_TENANT) is False


async def test_delete_refused_while_executions_exist(container) -> None:
    from jarvis.domain.execution import RunResult

    wf = await container.workflows.create(_workflow("ran-wf"))
    # D41: a workflow run is a row in agent_executions with the workflow id.
    await container.executions.create_run(
        RunResult(
            run_id=str(uuid4()),
            agent_id=wf.id,
            agent_version_id="unused",
            status="succeeded",
            input="go",
            tenant_id=TENANT,
        )
    )
    assert await container.workflows.has_executions(wf.id) is True
    assert await container.workflows.delete(wf.id, tenant_id=TENANT) is False


async def test_delete_without_executions_removes_versions_too(container) -> None:
    wf = await container.workflows.create(_workflow("clean-wf"))
    assert await container.workflows.delete(wf.id, tenant_id=TENANT) is True
    assert await container.workflows.get(wf.id, tenant_id=TENANT) is None
    assert await container.workflows.list_versions(wf.id, tenant_id=TENANT) == []


async def test_edges_roundtrip_through_the_snapshot(container) -> None:
    wf = WorkflowDefinition(
        id=str(uuid4()),
        name="graph-wf",
        nodes=[
            WorkflowNode(
                id="a",
                type="agent",
                config=AgentNodeConfig(agent_id="a1", input_template="{{input}}"),
            ),
            WorkflowNode(
                id="b",
                type="agent",
                config=AgentNodeConfig(agent_id="a2", input_template="{{node.a}}"),
            ),
        ],
        edges=[WorkflowEdge(from_node="a", to_node="b")],
        start_node_id="a",
    )
    await container.workflows.create(wf)
    loaded = await container.workflows.get(wf.id, tenant_id=TENANT)
    assert loaded is not None and loaded.edges == wf.edges
    version = await container.workflows.latest_version(wf.id, tenant_id=TENANT)
    assert version is not None
    restored = await container.workflows.get_version_by_id(version.id)
    assert restored is not None
    assert restored.snapshot == loaded
