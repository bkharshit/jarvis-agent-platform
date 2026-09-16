"""The event-trigger observer (S13, ADR 0019 §5, D61) — a post-commit
callback on `SqlExecutionRepo.finish_run`: when ANY run of the tenant
reaches a terminal status, enabled event triggers of that tenant are
matched and fire through the same `TriggerFireService` as cron and
webhook. The observer NEVER affects the finalizing run: per-trigger
failures log + stamp `last_error`, and the whole callback is wrapped
best-effort by `finish_run` (rule 2 — a trigger side-effect can never
break a terminal write).

The depth guard reads the PERSISTED `metadata["trigger"]["depth"]` of the
finalized run (the repo passes the row-rebuilt result): a trigger-fired
run carries depth ≥ 1, so at the default `trigger_max_depth` a
trigger-fired run's completion fires nothing — no infinite loops through
chains of event triggers. Sub-agent / handoff / eval children are
ordinary runs and DO fire event triggers (bounded by the same guard):
the S14 producers copy the parent's trigger provenance verbatim
(`with_trigger_provenance`), so a child of a trigger-fired run inherits
its depth too.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from jarvis.domain.execution import TERMINAL_STATUSES, RunResult
from jarvis.domain.trigger import EventConfig, Trigger
from jarvis.runtime.trigger_firing import TriggerFireService

logger = logging.getLogger("jarvis.triggers")

__all__ = ["TriggerService"]


class EventTriggerSource(Protocol):
    """Structural view of `SqlTriggerRepo`: the tenant's enabled event
    triggers plus the honest failure stamp."""

    async def enabled_event_triggers(self, tenant_id: str) -> list[Trigger]: ...

    async def record_fire(
        self, trigger_id: str, *, tenant_id: str, fired_at: datetime, error: str | None = None
    ) -> None: ...


class TriggerService:
    """One per container. Called by the persistence seam after every
    terminal write; stateless besides its config."""

    def __init__(
        self,
        *,
        triggers: EventTriggerSource,
        firing: TriggerFireService,
        max_depth: int,
        max_output_chars: int = 8_000,
    ) -> None:
        self._triggers = triggers
        self._firing = firing
        self._max_depth = max_depth
        self._max_output_chars = max_output_chars

    async def on_run_finalized(self, result: RunResult) -> None:
        """`result` is the row-rebuilt `RunResult` (status AND metadata are
        the persisted truth). Non-terminal or tenant-less runs match
        nothing; the depth guard reads `result.metadata`."""
        if result.status not in TERMINAL_STATUSES:
            return
        if result.tenant_id is None:
            # Triggers are strictly tenant-owned — a tenant-less run has no
            # trigger set to match (the pre-S2 default-tenant runs predate S13).
            return
        depth = _depth_of(result.metadata)
        if depth >= self._max_depth:
            logger.debug(
                "run %s at trigger depth %s ≥ max %s — event triggers skip it",
                result.run_id,
                depth,
                self._max_depth,
            )
            return
        triggers = await self._triggers.enabled_event_triggers(result.tenant_id)
        now = datetime.now(UTC)
        for trigger in triggers:
            if not _matches(trigger, result):
                continue
            try:
                await self._firing.fire(
                    trigger,
                    context_vars={
                        "run_id": result.run_id,
                        "agent_id": result.agent_id,
                        "status": result.status,
                        "output": (result.final_message or "")[: self._max_output_chars],
                    },
                    incoming_depth=depth,
                )
            except Exception as exc:  # noqa: BLE001 — one bad trigger never stops the rest
                logger.warning(
                    "event trigger %s (%s) failed to fire from run %s: %s",
                    trigger.id,
                    trigger.name,
                    result.run_id,
                    exc,
                )
                await self._stamp(trigger, now, error=str(exc))

    async def _stamp(self, trigger: Trigger, fired_at: datetime, *, error: str) -> None:
        try:
            await self._triggers.record_fire(
                trigger.id, tenant_id=trigger.tenant_id, fired_at=fired_at, error=error
            )
        except Exception:  # noqa: BLE001 — the stamp is best-effort honesty
            logger.exception("recording fire failure for trigger %s failed", trigger.id)


def _depth_of(metadata: Any) -> int:
    """The finalized run's own trigger depth — 0 when it was not
    trigger-fired (manual, eval, webhook-root, ...)."""
    if not isinstance(metadata, dict):
        return 0
    trigger = metadata.get("trigger")
    if not isinstance(trigger, dict):
        return 0
    depth = trigger.get("depth", 0)
    return depth if isinstance(depth, int) and depth >= 0 else 0


def _matches(trigger: Trigger, result: RunResult) -> bool:
    config = trigger.config
    if not isinstance(config, EventConfig):
        return False
    if config.status != "any" and config.status != result.status:
        return False
    if config.source_agent_id is not None and config.source_agent_id != result.agent_id:
        return False
    return True
