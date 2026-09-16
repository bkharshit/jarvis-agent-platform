"""Trigger domain types (S13, ADR 0019 §1, D57): the closed config union,
kind/config agreement, the webhook-only secret discipline, and the additive
`AuthMode "system"` member the firing service rides (ADR 0019 §2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jarvis.domain.auth import Principal
from jarvis.domain.trigger import (
    CronConfig,
    EventConfig,
    Trigger,
    WebhookConfig,
)
from jarvis.persistence.cron import next_cron_fire, validate_cron_schedule
from jarvis.persistence.scoped import TenantScopedExecutions


def _trigger(**overrides: object) -> Trigger:
    fields: dict[str, object] = {
        "id": "trg-1",
        "tenant_id": "default",
        "name": "nightly",
        "kind": "cron",
        "agent_id": "agent-1",
        "config": CronConfig(schedule="0 3 * * *"),
    }
    fields.update(overrides)
    return Trigger(**fields)  # type: ignore[arg-type]


# --- config union -----------------------------------------------------------


def test_cron_config_parses_and_defaults_kind() -> None:
    config = CronConfig(schedule="*/5 * * * *")
    assert config.type == "cron"


def test_cron_config_requires_schedule() -> None:
    with pytest.raises(ValidationError):
        CronConfig()  # type: ignore[call-arg]


def test_webhook_config_is_empty_and_closed() -> None:
    assert WebhookConfig().type == "webhook"
    with pytest.raises(ValidationError):
        WebhookConfig(url="https://x")  # type: ignore[call-arg]


def test_event_config_status_literals_and_optional_source() -> None:
    config = EventConfig(status="failed", source_agent_id="agent-9")
    assert config.type == "event"
    assert EventConfig(status="any").source_agent_id is None
    for bad in ("running", "awaiting_input", "queued", "anything"):
        with pytest.raises(ValidationError):
            EventConfig(status=bad)  # type: ignore[arg-type]


def test_config_union_rejects_unknown_member_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        CronConfig(type="webhook", schedule="* * * * *")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CronConfig(schedule="* * * * *", timezone="UTC")  # type: ignore[call-arg]


# --- Trigger -------------------------------------------------------------


def test_trigger_defaults_and_config_kind_agreement() -> None:
    trigger = _trigger()
    assert trigger.version_policy == "latest"
    assert trigger.enabled is True
    assert trigger.agent_version_id is None
    assert trigger.secret is None
    assert trigger.next_fire_at is None
    assert trigger.last_error is None


def test_trigger_rejects_config_kind_mismatch() -> None:
    with pytest.raises(ValidationError, match="must agree"):
        _trigger(kind="webhook", config=CronConfig(schedule="* * * * *"))


def test_trigger_secret_is_webhook_only() -> None:
    envelope = {"v": "1", "key_id": "abc", "nonce": "n", "ct": "c"}
    webhook = _trigger(
        kind="webhook",
        config=WebhookConfig(),
        secret=dict(envelope),
        version_policy="pinned",
        agent_version_id="ver-1",
    )
    assert webhook.secret == envelope
    with pytest.raises(ValidationError, match="webhook"):
        _trigger(secret=dict(envelope))
    with pytest.raises(ValidationError, match="webhook"):
        _trigger(kind="event", config=EventConfig(status="any"), secret=dict(envelope))


def test_trigger_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _trigger(metadata={"trigger": {}})  # type: ignore[call-arg]


# --- AuthMode "system" (ADR 0019 §2, D58) -----------------------------------


def test_principal_accepts_system_mode() -> None:
    principal = Principal(tenant_id="default", mode="system")
    assert principal.is_anonymous is False
    assert principal.user_id is None
    assert principal.can_manage_members is False  # mode has no role branches


def test_principal_still_rejects_unknown_modes() -> None:
    with pytest.raises(ValidationError):
        Principal(tenant_id="default", mode="robot")  # type: ignore[arg-type]


# --- cron math (persistence/cron, D59) ---------------------------------------


def test_next_cron_fire_is_strictly_after_now() -> None:
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    assert next_cron_fire("0 12 * * *", now) == datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
    assert next_cron_fire("*/15 * * * *", now) == datetime(2026, 9, 16, 12, 15, 0, tzinfo=UTC)


def test_next_cron_fire_rejects_bad_expression_and_naive_now() -> None:
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_cron_fire("not a cron", now)
    with pytest.raises(ValueError):
        next_cron_fire("* * * * *", datetime(2026, 9, 16, 12, 0, 0))


def test_validate_cron_schedule_accepts_and_rejects() -> None:
    validate_cron_schedule("0 3 * * *")
    validate_cron_schedule("*/5 * * * *")
    with pytest.raises(ValueError, match="invalid cron expression"):
        validate_cron_schedule("bad schedule")


# --- list_runs keyword pass-through (byte-identical default) -----------------


class _RecordingExecutions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_runs(self, *args: object, **kwargs: object) -> list[object]:
        self.calls.append({"args": args, **kwargs})
        return []


async def test_scoped_list_runs_forwards_trigger_id_and_defaults_to_none() -> None:
    inner = _RecordingExecutions()
    scoped = TenantScopedExecutions(inner, tenant_id="default")  # type: ignore[arg-type]
    # Default call: the keyword exists but stays None — pre-S13 callers are
    # byte-identical.
    await scoped.list_runs(agent_id="a")
    assert inner.calls[-1]["trigger_id"] is None
    assert inner.calls[-1]["tenant_id"] == "default"
    await scoped.list_runs(trigger_id="trg-1")
    assert inner.calls[-1]["trigger_id"] == "trg-1"
