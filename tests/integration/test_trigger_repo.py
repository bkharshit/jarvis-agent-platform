"""SqlTriggerRepo integration (S13, ADR 0019 §1, D57/D59): table round-trip,
strict tenant scoping (no shared tier), the claim's SKIP LOCKED exclusivity,
and the executions `trigger_id` provenance filter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jarvis.domain.execution import RunResult
from jarvis.domain.trigger import CronConfig, EventConfig, Trigger
from jarvis.persistence.repositories import SqlExecutionRepo, SqlTriggerRepo

pytestmark = pytest.mark.db

TENANT = "default"  # seeded by migration 0004 / conftest truncate
OTHER_TENANT = "tenant-b"


def _cron(name: str = "nightly", *, schedule: str = "0 3 * * *", enabled: bool = True) -> Trigger:
    return Trigger(
        id=str(uuid4()),
        tenant_id=TENANT,
        name=name,
        kind="cron",
        agent_id="agent-1",
        config=CronConfig(schedule=schedule),
        enabled=enabled,
        next_fire_at=datetime.now(UTC) - timedelta(minutes=1),
    )


@pytest.fixture
def repo(container) -> SqlTriggerRepo:
    return SqlTriggerRepo(async_sessionmaker(container.engine, expire_on_commit=False))


@pytest.fixture
def executions(container) -> SqlExecutionRepo:
    return SqlExecutionRepo(async_sessionmaker(container.engine, expire_on_commit=False))


@pytest.fixture
def make_tenant(container):
    async def go(tenant_id: str) -> None:
        await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")

    return go


async def test_create_get_roundtrip(repo: SqlTriggerRepo) -> None:
    trigger = _cron()
    created = await repo.create(trigger, tenant_id=TENANT)
    assert created.tenant_id == TENANT
    loaded = await repo.get(trigger.id, tenant_id=TENANT)
    assert loaded == created


async def test_get_is_strictly_tenant_scoped(repo: SqlTriggerRepo, make_tenant) -> None:
    await make_tenant(OTHER_TENANT)
    trigger = await repo.create(_cron(), tenant_id=TENANT)
    assert await repo.get(trigger.id, tenant_id=OTHER_TENANT) is None
    assert await repo.list_triggers(tenant_id=OTHER_TENANT) == []


async def test_names_are_unique_per_tenant(repo: SqlTriggerRepo) -> None:
    await repo.create(_cron(name="dupe"), tenant_id=TENANT)
    with pytest.raises(IntegrityError):
        await repo.create(_cron(name="dupe"), tenant_id=TENANT)


async def test_update_replaces_mutable_fields(repo: SqlTriggerRepo) -> None:
    trigger = await repo.create(_cron(), tenant_id=TENANT)
    trigger.enabled = False
    trigger.name = "renamed"
    trigger.config = CronConfig(schedule="*/10 * * * *")
    trigger.next_fire_at = None
    updated = await repo.update(trigger, tenant_id=TENANT)
    assert updated.enabled is False
    assert updated.name == "renamed"
    assert updated.config.schedule == "*/10 * * * *"
    # A foreign-tenant update is a LookupError, never a cross-tenant write.
    with pytest.raises(LookupError):
        await repo.update(trigger, tenant_id=OTHER_TENANT)


async def test_delete_is_always_allowed_and_scoped(repo: SqlTriggerRepo) -> None:
    trigger = await repo.create(_cron(), tenant_id=TENANT)
    assert await repo.delete(trigger.id, tenant_id=OTHER_TENANT) is False
    assert await repo.delete(trigger.id, tenant_id=TENANT) is True
    assert await repo.get(trigger.id, tenant_id=TENANT) is None


async def test_claim_due_crons_advances_exactly_once(repo: SqlTriggerRepo) -> None:
    due = await repo.create(_cron(name="due"), tenant_id=TENANT)
    not_due = await repo.create(
        _cron(name="future", schedule="0 3 * * *", enabled=True), tenant_id=TENANT
    )
    await repo.update(
        not_due.model_copy(update={"next_fire_at": datetime.now(UTC) + timedelta(hours=1)}),
        tenant_id=TENANT,
    )
    now = datetime.now(UTC)
    claimed = await repo.claim_due_crons(now, limit=10)
    assert [t.id for t in claimed] == [due.id]
    fired = claimed[0]
    assert fired.last_fired_at == now
    # next_fire_at advanced strictly after `now` from the same schedule.
    assert fired.next_fire_at is not None and fired.next_fire_at > now
    assert fired.last_error is None
    # A second claim (another scheduler instance) gets nothing — the row
    # moved out of the due window.
    assert await repo.claim_due_crons(now, limit=10) == []


async def test_claim_requires_enabled(repo: SqlTriggerRepo) -> None:
    disabled = _cron(name="off", enabled=False)
    await repo.create(disabled, tenant_id=TENANT)
    assert await repo.claim_due_crons(datetime.now(UTC), limit=10) == []


async def test_claim_exclusive_under_concurrency(repo: SqlTriggerRepo) -> None:
    """Two racing claimants never double-claim a row (the SKIP LOCKED idiom,
    the ADR 0019 §3 exactly-once argument)."""
    for i in range(6):
        await repo.create(_cron(name=f"racer-{i}"), tenant_id=TENANT)
    now = datetime.now(UTC)
    first, second = await asyncio.gather(
        repo.claim_due_crons(now, limit=10),
        repo.claim_due_crons(now, limit=10),
    )
    first_ids = [t.id for t in first]
    second_ids = [t.id for t in second]
    assert len(first_ids) + len(second_ids) == 6
    assert not set(first_ids) & set(second_ids)


async def test_record_fire_stamps_row(repo: SqlTriggerRepo) -> None:
    trigger = await repo.create(_cron(), tenant_id=TENANT)
    fired_at = datetime.now(UTC)
    await repo.record_fire(trigger.id, tenant_id=TENANT, fired_at=fired_at, error="boom")
    loaded = await repo.get(trigger.id, tenant_id=TENANT)
    assert loaded is not None
    assert loaded.last_fired_at == fired_at
    assert loaded.last_error == "boom"


async def test_enabled_event_triggers_filters(repo: SqlTriggerRepo) -> None:
    event_on = await repo.create(
        Trigger(
            id=str(uuid4()),
            tenant_id=TENANT,
            name="on-failed",
            kind="event",
            agent_id="agent-1",
            config=EventConfig(status="failed"),
        ),
        tenant_id=TENANT,
    )
    await repo.create(
        Trigger(
            id=str(uuid4()),
            tenant_id=TENANT,
            name="off-event",
            kind="event",
            agent_id="agent-1",
            config=EventConfig(status="any"),
            enabled=False,
        ),
        tenant_id=TENANT,
    )
    await repo.create(_cron(name="not-event"), tenant_id=TENANT)
    triggers = await repo.enabled_event_triggers(TENANT)
    assert [t.id for t in triggers] == [event_on.id]


async def test_run_row_carries_trigger_id_from_metadata(
    repo: SqlTriggerRepo, executions: SqlExecutionRepo
) -> None:
    """The provenance column is lifted from metadata["trigger"] at the
    serialization boundary (D62) and the executions filter matches it."""
    trigger = await repo.create(
        Trigger(
            id=str(uuid4()),
            tenant_id=TENANT,
            name="watcher",
            kind="event",
            agent_id="agent-1",
            config=EventConfig(status="succeeded"),
        ),
        tenant_id=TENANT,
    )
    fired = RunResult(
        run_id=str(uuid4()),
        agent_id="agent-1",
        status="queued",
        agent_version_id="ver-1",
        tenant_id=TENANT,
        trace_id=str(uuid4()),
        metadata={"trigger": {"trigger_id": trigger.id, "kind": "event", "name": "watcher"}},
    )
    manual = RunResult(
        run_id=str(uuid4()),
        agent_id="agent-1",
        status="queued",
        agent_version_id="ver-1",
        tenant_id=TENANT,
        trace_id=str(uuid4()),
    )
    await executions.create_run(fired)
    await executions.create_run(manual)

    runs = await executions.list_runs(trigger_id=trigger.id, tenant_id=TENANT)
    assert [r.run_id for r in runs] == [fired.run_id]
    # Runs without trigger metadata keep NULL — the default filter is a no-op.
    everything = await executions.list_runs(tenant_id=TENANT)
    assert {r.run_id for r in everything} == {fired.run_id, manual.run_id}
