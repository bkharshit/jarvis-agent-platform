"""TriggerFireService (S13, ADR 0019 §2, D58): version resolution (pinned
replays forever / latest resolves at fire time), the per-kind empty-template
defaults, template rendering with the context vars, and the fired run's
composition — system principal, fresh trace, platform deadline, no session,
and the `metadata["trigger"]` provenance namespace with depth = incoming+1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig
from jarvis.domain.trigger import CronConfig, EventConfig, Trigger, WebhookConfig
from jarvis.runtime.trigger_firing import TriggerFireError, TriggerFireService


class FakeAgents:
    def __init__(self, versions: list[AgentVersion]):
        self._versions = versions
        self.latest_lookups: list[tuple[str, str | None]] = []
        self.pin_lookups: list[str] = []

    async def latest_version(self, agent_id: str, *, tenant_id: str | None = None):
        self.latest_lookups.append((agent_id, tenant_id))
        for version in self._versions:
            if version.agent_id == agent_id:
                return version
        return None

    async def get_version_by_id(self, version_id: str):
        self.pin_lookups.append(version_id)
        for version in self._versions:
            if version.id == version_id:
                return version
        return None


class FakeExecutions:
    def __init__(self) -> None:
        self.written: list[tuple[Any, Any]] = []

    async def create_queued_run(self, result, message) -> None:
        self.written.append((result, message))


def _version(agent_id: str = "watched", version: int = 1) -> AgentVersion:
    return AgentVersion(
        id=f"ver-{uuid4().hex[:8]}",
        agent_id=agent_id,
        version=version,
        snapshot=AgentDefinition(
            id=agent_id,
            name="Watched",
            model=ModelRef(provider="mock", model="mock-1"),
            strategy=StrategyConfig(type="function_calling"),
        ),
    )


def _trigger(**overrides: object) -> Trigger:
    fields: dict[str, object] = {
        "id": "trg-1",
        "tenant_id": "default",
        "name": "nightly",
        "kind": "cron",
        "agent_id": "watched",
        "config": CronConfig(schedule="0 3 * * *"),
    }
    fields.update(overrides)
    return Trigger(**fields)  # type: ignore[arg-type]


def _service(agents: FakeAgents, executions: FakeExecutions) -> TriggerFireService:
    return TriggerFireService(
        executions=executions,  # type: ignore[arg-type]
        agents=agents,  # type: ignore[arg-type]
        run_timeout_seconds=120.0,  # a fresh platform budget, not Settings' None default
    )


# --- version resolution ------------------------------------------------------


async def test_latest_policy_resolves_published_version_at_fire_time() -> None:
    version = _version()
    agents, executions = FakeAgents([version]), FakeExecutions()
    run_id = await _service(agents, executions).fire(_trigger(), context_vars={})
    assert run_id == executions.written[0][1].run_id
    assert agents.latest_lookups == [("watched", "default")]
    assert executions.written[0][1].agent_version_id == version.id


async def test_latest_policy_with_no_published_version_fails_honestly() -> None:
    agents, executions = FakeAgents([]), FakeExecutions()
    with pytest.raises(TriggerFireError, match="no published version"):
        await _service(agents, executions).fire(_trigger(), context_vars={})
    assert executions.written == []


async def test_pinned_policy_replays_the_stamped_version() -> None:
    pinned = _version(version=2)
    agents, executions = FakeAgents([pinned]), FakeExecutions()
    trigger = _trigger(
        version_policy="pinned", agent_version_id=pinned.id, config=CronConfig(schedule="* * * * *")
    )
    await _service(agents, executions).fire(trigger, context_vars={})
    assert agents.pin_lookups == [pinned.id]
    assert agents.latest_lookups == []  # never consulted
    assert executions.written[0][1].agent_version_id == pinned.id


async def test_pinned_policy_with_missing_version_fails_honestly() -> None:
    agents, executions = FakeAgents([]), FakeExecutions()
    trigger = _trigger(version_policy="pinned", agent_version_id="ver-gone")
    with pytest.raises(TriggerFireError, match="ver-gone"):
        await _service(agents, executions).fire(trigger, context_vars={})


async def test_pinned_policy_without_a_stamp_fails_honestly() -> None:
    agents, executions = FakeAgents([]), FakeExecutions()
    with pytest.raises(TriggerFireError, match="no agent_version_id"):
        await _service(agents, executions).fire(_trigger(version_policy="pinned"), context_vars={})


# --- input templates ---------------------------------------------------------


async def test_template_renders_trigger_and_context_vars() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    trigger = _trigger(
        kind="event",
        config=EventConfig(status="failed"),
        input_template=(
            "Run {{run_id}} ended {{status}} (fired by {{trigger_name}}); {{unknown_var}} stays."
        ),
    )
    await _service(agents, executions).fire(
        trigger,
        context_vars={"run_id": "run-9", "status": "failed", "agent_id": "watched"},
    )
    message = executions.written[0][1]
    assert message.input == ("Run run-9 ended failed (fired by nightly); {{unknown_var}} stays.")


async def test_empty_template_gets_the_cron_default() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    await _service(agents, executions).fire(_trigger(), context_vars={})
    message = executions.written[0][1]
    assert message.input.startswith("Scheduled run of agent watched by trigger 'nightly' at ")
    assert message.input.endswith(".")


async def test_empty_template_gets_the_webhook_payload_default() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    trigger = _trigger(kind="webhook", config=WebhookConfig())
    await _service(agents, executions).fire(trigger, context_vars={"payload": '{"alert": true}'})
    assert executions.written[0][1].input == '{"alert": true}'
    # A manual fire carries no payload — the run still gets an input.
    await _service(agents, executions).fire(trigger, context_vars={})
    assert executions.written[1][1].input == "{}"


async def test_empty_template_gets_the_event_default() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    trigger = _trigger(kind="event", config=EventConfig(status="any"))
    await _service(agents, executions).fire(
        trigger, context_vars={"run_id": "run-9", "agent_id": "watched", "status": "failed"}
    )
    assert executions.written[0][1].input == (
        "Run run-9 of agent watched finished with status failed."
    )


# --- fired-run composition ---------------------------------------------------


async def test_fired_run_shape_is_the_child_shape_with_no_parent() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    trigger = _trigger(
        kind="event",
        config=EventConfig(status="succeeded"),
        input_template="re: {{output}}",
    )
    run_id = await _service(agents, executions).fire(
        trigger,
        context_vars={
            "run_id": "run-9",
            "status": "succeeded",
            "agent_id": "watched",
            "output": "ok",
        },
        incoming_depth=3,
    )
    result, message = executions.written[0]
    assert run_id == message.run_id == result.run_id
    assert result.status == "queued"
    assert result.metadata == message.metadata
    assert message.kind == "agent"
    assert message.tenant_id == "default"
    assert message.session_id is None  # memory-less fired runs (the S14 child precedent)
    assert message.parent_run_id is None
    assert message.trace_id != "run-9"  # a fresh trace, not the observed run's
    # The synthesized service principal (D58).
    principal = message.principal
    assert principal is not None and principal.tenant_id == "default" and principal.mode == "system"
    # A fresh platform budget from the configured timeout, not an inherited one.
    assert message.deadline is not None
    remaining = (message.deadline - datetime.now(UTC)).total_seconds()
    assert 110 < remaining <= 120
    # The provenance namespace + depth = incoming + 1 (§6's guard reads it).
    assert message.metadata == {
        "trigger": {
            "trigger_id": "trg-1",
            "kind": "event",
            "name": "nightly",
            "depth": 4,
        }
    }


async def test_cron_and_webhook_fires_are_depth_zero_born() -> None:
    agents, executions = FakeAgents([_version()]), FakeExecutions()
    await _service(agents, executions).fire(_trigger(), context_vars={})
    assert executions.written[0][1].metadata["trigger"]["depth"] == 1


def test_fired_run_metadata_is_not_writable_through_the_trigger() -> None:
    """The provenance namespace is minted by the service — a trigger row can
    never smuggle fields into it (extra="forbid" on the domain model)."""
    with pytest.raises(ValidationError):
        _trigger(metadata={"trigger": {"trigger_id": "x"}})  # type: ignore[call-arg]
