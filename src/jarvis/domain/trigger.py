"""Trigger definitions (S13, ADR 0019 §1) — pure Pydantic, no IO.

A trigger is a tenant-owned rule that starts ordinary runs on its own: a
cron schedule, an inbound webhook, or a watcher on run outcomes. The config
union is a closed, discriminated set (the `domain/mcp.py` shape); the row's
`kind` column is authoritative and every config member must agree with it.

Cron expressions are parsed with croniter at the API/firing boundary (422
on a bad expression), NOT here — `domain/` imports only pydantic/stdlib
(CLAUDE.md rule 1). The secrets discipline is the stored-credential one:
`secret` carries only the AES-GCM envelope, write-only through the API
(never in any GET response; D29/D30).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TriggerKind = Literal["cron", "webhook", "event"]
TriggerVersionPolicy = Literal["pinned", "latest"]

# The closed event-condition set (ADR 0019 §5): a terminal-status match,
# optionally scoped to one source agent. `any` = every terminal status.
EVENT_STATUSES = ("succeeded", "failed", "cancelled", "timed_out", "any")
EventStatus = Literal["succeeded", "failed", "cancelled", "timed_out", "any"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CronConfig(_Model):
    """A five-field cron expression. UTC-only (the codebase is uniformly
    UTC); per-trigger IANA timezones are a recorded deferral (ADR 0019 §8).
    Parse validation lives at the API/firing boundary via croniter."""

    type: Literal["cron"] = "cron"
    schedule: str = Field(min_length=1)


class WebhookConfig(_Model):
    """No fields v1 — the member exists so the discriminated parse stays
    closed; the ingest route's behavior is fixed by kind."""

    type: Literal["webhook"] = "webhook"


class EventConfig(_Model):
    """A run of this tenant (optionally only `source_agent_id`'s runs)
    reached this terminal status."""

    type: Literal["event"] = "event"
    status: EventStatus
    source_agent_id: str | None = None


TriggerConfig = Annotated[CronConfig | WebhookConfig | EventConfig, Field(discriminator="type")]


class Trigger(_Model):
    """One trigger rule. `agent_id` is a loose reference (no FK — the
    mcp-name precedent): firing fails honestly with `last_error` when the
    target is gone. `version_policy` decides the snapshot at fire time:
    `pinned` replays the route-stamped `agent_version_id` forever (D1);
    `latest` resolves the target's current published version per fire — the
    explicit opt-in to drift."""

    id: str
    tenant_id: str
    name: str
    kind: TriggerKind
    agent_id: str
    agent_version_id: str | None = None
    version_policy: TriggerVersionPolicy = "latest"
    input_template: str | None = None
    enabled: bool = True
    config: TriggerConfig
    # Webhook only: the AES-GCM envelope {v, key_id, nonce, ct} of the
    # server-generated HMAC secret. Plaintext exists exactly once (create /
    # rotate responses); this field is never serialized in any GET (D29/D30).
    secret: dict[str, Any] | None = None
    next_fire_at: datetime | None = None
    last_fired_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _config_matches_kind(self) -> Trigger:
        if self.config.type != self.kind:
            raise ValueError(
                f"config is a {self.config.type!r} config but the trigger kind is "
                f"{self.kind!r} — they must agree"
            )
        if self.kind != "webhook" and self.secret is not None:
            raise ValueError("only webhook triggers carry a secret")
        return self


__all__ = [
    "EVENT_STATUSES",
    "CronConfig",
    "EventConfig",
    "Trigger",
    "TriggerConfig",
    "TriggerKind",
    "TriggerVersionPolicy",
    "WebhookConfig",
]
