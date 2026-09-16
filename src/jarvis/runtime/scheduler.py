"""The cron scheduler loop (S13, ADR 0019 §3, D59) — Postgres truth, the
`_sweep_loop` idiom: sleep, claim, fire, repeat. The claim is at-most-once
(FOR UPDATE SKIP LOCKED, committed FIRST), the enqueue happens after, per
trigger: a crash in between misses one fire (the next is already scheduled)
and an enqueue failure stamps `last_error` on the row — visible in the UI,
never a retry storm. `fire_due`'s exceptions are contained (log + continue);
the loop never dies on one bad trigger.

Runs only inside `serve`'s lifespan (gated by `trigger_scheduler_enabled`)
— it only enqueues, so it is independent of `embedded_worker`; the
standalone `jarvis worker` CLI does not run it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from jarvis.domain.trigger import Trigger
from jarvis.runtime.trigger_firing import TriggerFireService

logger = logging.getLogger("jarvis.scheduler")

__all__ = ["TriggerScheduler"]


class TriggerClaimer(Protocol):
    """Structural view of `SqlTriggerRepo`: the SKIP LOCKED claim plus the
    honest failure stamp. The claim already advances `next_fire_at` and
    clears `last_error` — only the enqueue FAILURE path writes again."""

    async def claim_due_crons(self, now: datetime, limit: int = 10) -> list[Trigger]: ...

    async def record_fire(
        self, trigger_id: str, *, tenant_id: str, fired_at: datetime, error: str | None = None
    ) -> None: ...


class TriggerScheduler:
    """One loop per serve process. Multiple instances are safe by
    construction — the row lock makes exactly one claimant win."""

    def __init__(
        self, *, triggers: TriggerClaimer, firing: TriggerFireService, poll_seconds: float
    ) -> None:
        self._triggers = triggers
        self._firing = firing
        self._poll_seconds = poll_seconds

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            # Contained: one bad cycle never kills the loop.
            try:
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trigger scheduler cycle failed — continuing")

    async def fire_due(self) -> int:
        """Claim due crons and enqueue one run each; returns the number of
        successful fires. A failing fire stamps `last_error` on the row and
        the loop moves on — the next schedule slot is the retry."""
        now = datetime.now(UTC)
        claimed = await self._triggers.claim_due_crons(now)
        fired = 0
        for trigger in claimed:
            try:
                await self._firing.fire(trigger, context_vars={})
            except Exception as exc:  # noqa: BLE001 — one bad trigger never stops the rest
                logger.warning(
                    "cron trigger %s (%s) failed to fire: %s", trigger.id, trigger.name, exc
                )
                await self._stamp(trigger, now, error=str(exc))
            else:
                fired += 1
                # Success needs no write — the claim already advanced
                # next_fire_at, set last_fired_at and cleared last_error.
        return fired

    async def _stamp(self, trigger: Trigger, fired_at: datetime, *, error: str) -> None:
        try:
            await self._triggers.record_fire(
                trigger.id, tenant_id=trigger.tenant_id, fired_at=fired_at, error=error
            )
        except Exception:  # noqa: BLE001 — the stamp is best-effort honesty
            logger.exception("recording fire failure for trigger %s failed", trigger.id)
