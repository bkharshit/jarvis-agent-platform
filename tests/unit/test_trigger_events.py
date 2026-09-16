"""TriggerService.on_run_finalized unit tests (S13, D61): the condition
matrix, the depth guard reading persisted metadata, and the honest
failure containment — a trigger side-effect never raises out of the
observer, and never affects the finalizing run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis.domain.execution import RunResult
from jarvis.domain.trigger import Trigger
from jarvis.runtime.trigger_events import TriggerService


class FakeTriggers:
    def __init__(self, triggers: list[Trigger]) -> None:
        self._triggers = triggers
        self.stamped: list[tuple[str, str]] = []

    async def enabled_event_triggers(self, tenant_id: str) -> list[Trigger]:
        return self._triggers

    async def record_fire(
        self, trigger_id: str, *, tenant_id: str, fired_at: datetime, error: str | None = None
    ) -> None:
        assert error is not None  # only failures stamp
        self.stamped.append((trigger_id, error))


class FakeFiring:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []  # (trigger_id, incoming_depth)

    async def fire(
        self, trigger: Trigger, *, context_vars: dict[str, Any], incoming_depth: int = 0
    ) -> str:
        self.calls.append((trigger.id, incoming_depth))
        return "run-fired"


def _run(**overrides: Any) -> RunResult:
    base: dict[str, Any] = {
        "run_id": "r-1",
        "agent_id": "agent-a",
        "status": "succeeded",
        "tenant_id": "default",
    }
    base.update(overrides)
    return RunResult(**base)


def _event_trigger(trigger_id: str = "t-1", **cfg: Any) -> Trigger:
    config = {"type": "event", "status": "any"} | cfg
    return Trigger(
        id=trigger_id,
        tenant_id="default",
        name="on-finish",
        kind="event",
        agent_id="agent-b",
        config=config,  # type: ignore[arg-type]
    )


def _service(
    triggers: list[Trigger], *, firing: FakeFiring | None = None, max_depth: int = 1
) -> tuple[TriggerService, FakeFiring, FakeTriggers]:
    firing = firing or FakeFiring()
    fake_triggers = FakeTriggers(triggers)
    return (
        TriggerService(triggers=fake_triggers, firing=firing, max_depth=max_depth),
        firing,
        fake_triggers,
    )


async def test_matching_status_and_any_fire_with_source_run_context() -> None:
    service, firing, _ = _service([_event_trigger(status="any")])
    await service.on_run_finalized(
        _run(final_message="the answer", metadata={"trigger": {"depth": 0, "trigger_id": "t-0"}})
    )
    assert firing.calls == [("t-1", 0)]
    # the fired run carries depth = incoming + 1 (the firing service stamps it;
    # here we assert the observer passed the finalized run's own depth)
    assert firing.calls[0][1] == 0


async def test_status_mismatch_does_not_fire() -> None:
    service, firing, _ = _service([_event_trigger(status="failed")])
    await service.on_run_finalized(_run())
    assert firing.calls == []


async def test_status_any_matches_every_terminal_status() -> None:
    service, firing, _ = _service([_event_trigger()])
    for status in ("succeeded", "failed", "cancelled", "timed_out"):
        await service.on_run_finalized(_run(run_id=f"r-{status}", status=status))
    assert [c[0] for c in firing.calls] == ["t-1"] * 4


async def test_source_agent_filter() -> None:
    service, firing, _ = _service([_event_trigger(source_agent_id="agent-other")])
    await service.on_run_finalized(_run(agent_id="agent-a"))
    assert firing.calls == []
    await service.on_run_finalized(_run(agent_id="agent-other"))
    assert firing.calls == [("t-1", 0)]


async def test_depth_guard_refuses_a_trigger_fired_run_at_max_depth() -> None:
    service, firing, _ = _service([_event_trigger()], max_depth=1)
    # a trigger-fired run carries depth ≥ 1 in PERSISTED metadata
    await service.on_run_finalized(_run(metadata={"trigger": {"depth": 1, "trigger_id": "t-0"}}))
    assert firing.calls == []
    # depth 2 (a grandchild) is refused too — the guard is >=
    await service.on_run_finalized(_run(metadata={"trigger": {"depth": 2, "trigger_id": "t-0"}}))
    assert firing.calls == []


async def test_higher_max_depth_lets_a_depth_one_run_refire() -> None:
    service, firing, _ = _service([_event_trigger()], max_depth=2)
    await service.on_run_finalized(_run(metadata={"trigger": {"depth": 1, "trigger_id": "t-0"}}))
    assert firing.calls == [("t-1", 1)]


async def test_non_terminal_and_tenantless_runs_match_nothing() -> None:
    service, firing, _ = _service([_event_trigger()])
    await service.on_run_finalized(_run(status="running"))
    await service.on_run_finalized(_run(status="queued"))
    await service.on_run_finalized(_run(status="awaiting_input"))
    await service.on_run_finalized(_run(tenant_id=None))
    assert firing.calls == []


async def test_fire_failure_stamps_last_error_and_never_raises() -> None:
    class ExplodingFiring(FakeFiring):
        async def fire(
            self, trigger: Trigger, *, context_vars: dict[str, Any], incoming_depth: int = 0
        ) -> str:
            raise RuntimeError("target agent vanished")

    exploding = ExplodingFiring()
    fake_triggers = FakeTriggers([_event_trigger()])
    service = TriggerService(triggers=fake_triggers, firing=exploding, max_depth=1)
    # the observer swallows — the finalizing run is unaffected
    await service.on_run_finalized(_run())
    assert fake_triggers.stamped == [("t-1", "target agent vanished")]


async def test_depth_guard_tolerates_malformed_metadata() -> None:
    service, firing, _ = _service([_event_trigger()])
    for bad in ({}, {"trigger": "junk"}, {"trigger": {"depth": "two"}}, {"trigger": {"depth": -1}}):
        await service.on_run_finalized(_run(metadata=bad))
    assert firing.calls == [("t-1", 0)] * 4  # each treated as depth 0
