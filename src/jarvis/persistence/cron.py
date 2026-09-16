"""Cron schedule math (S13, ADR 0019 §3, D59) — the one croniter seam.

Lives in persistence/ because its consumers are the repo's claim advance
(`claim_due_crons`) and the API boundary (parse validation + `next_fire_at`
stamping at create/enable). All math is `croniter(schedule, now)`: after
downtime a trigger fires once and continues from now (catch-up = fire once,
never once-per-missed-slot). Five-field expressions, UTC-only; per-trigger
IANA timezones are a recorded deferral (ADR 0019 §8).
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import croniter


def next_cron_fire(schedule: str, now: datetime) -> datetime:
    """The schedule's next fire strictly after `now`. Raises ValueError on
    an unparseable expression (the API maps to 422)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (the platform is uniformly UTC)")
    next_at: datetime = croniter(schedule, now).get_next(datetime)
    return next_at


def validate_cron_schedule(schedule: str) -> None:
    """422 at the boundary: raise ValueError when croniter rejects the
    expression (never store an unfireable trigger)."""
    try:
        croniter(schedule, datetime.now(UTC))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid cron expression {schedule!r}: {exc}") from None


__all__ = ["next_cron_fire", "validate_cron_schedule"]
