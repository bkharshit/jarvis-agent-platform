"""TriggerScheduler (S13, ADR 0019 §3, D59): the claim-first fire loop —
one enqueue per claimed cron, a failing fire stamps `last_error` and the
loop moves on, success writes nothing (the claim already advanced the
row), and `run_forever` survives a bad cycle.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from jarvis.domain.trigger import CronConfig, Trigger
from jarvis.runtime.scheduler import TriggerScheduler


class FakeTriggers:
    def __init__(self, claimed: list[Trigger] | None = None, claim_error: Exception | None = None):
        self.claimed = claimed or []
        self.claim_error = claim_error
        self.claims: list[datetime] = []
        self.stamps: list[tuple[str, str, datetime, str | None]] = []

    async def claim_due_crons(self, now: datetime, limit: int = 10) -> list[Trigger]:
        self.claims.append(now)
        if self.claim_error is not None:
            raise self.claim_error
        return self.claimed

    async def record_fire(
        self, trigger_id: str, *, tenant_id: str, fired_at: datetime, error: str | None = None
    ) -> None:
        self.stamps.append((trigger_id, tenant_id, fired_at, error))


class FakeFiring:
    def __init__(self, failures: dict[str, Exception] | None = None):
        self.failures = failures or {}
        self.fires: list[tuple[str, dict[str, Any]]] = []

    async def fire(self, trigger: Trigger, *, context_vars: dict[str, Any], **kw: Any) -> str:
        self.fires.append((trigger.id, context_vars))
        if trigger.id in self.failures:
            raise self.failures[trigger.id]
        return "run-1"


def _trigger(name: str = "nightly") -> Trigger:
    return Trigger(
        id=f"trg-{name}",
        tenant_id="default",
        name=name,
        kind="cron",
        agent_id="agent-1",
        config=CronConfig(schedule="0 3 * * *"),
    )


def _scheduler(triggers: FakeTriggers, firing: FakeFiring) -> TriggerScheduler:
    return TriggerScheduler(
        triggers=triggers,  # type: ignore[arg-type]
        firing=firing,  # type: ignore[arg-type]
        poll_seconds=0.01,
    )


async def test_fire_due_fires_every_claimed_trigger_with_no_context() -> None:
    triggers, firing = FakeTriggers([_trigger("a"), _trigger("b")]), FakeFiring()
    fired = await _scheduler(triggers, firing).fire_due()
    assert fired == 2
    assert firing.fires == [("trg-a", {}), ("trg-b", {})]
    assert triggers.stamps == []  # the claim already stamped the rows


async def test_a_failing_fire_stamps_last_error_and_the_rest_still_fire() -> None:
    triggers = FakeTriggers([_trigger("bad"), _trigger("good")])
    firing = FakeFiring(failures={"trg-bad": RuntimeError("target agent gone")})
    fired = await _scheduler(triggers, firing).fire_due()
    assert fired == 1
    assert [t for t, _, _, _ in triggers.stamps] == ["trg-bad"]
    trigger_id, tenant_id, _, error = triggers.stamps[0]
    assert tenant_id == "default"
    assert "target agent gone" in error


async def test_a_failing_claim_cycle_is_contained() -> None:
    """A DB hiccup during the claim neither fires nor kills the loop."""
    triggers, firing = FakeTriggers(claim_error=RuntimeError("db down")), FakeFiring()
    scheduler = _scheduler(triggers, firing)
    task = asyncio.create_task(scheduler.run_forever())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert firing.fires == []
    assert len(triggers.claims) >= 2  # the loop kept ticking through failures
