"""Run limits — the orchestrator's loop guard (ADR 0004)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jarvis.domain.execution import ExecutionContext

LIMIT_MAX_ITERATIONS = 32


@dataclass(frozen=True)
class RunLimits:
    max_iterations: int = 8
    max_total_tokens: int | None = None

    def exceeded(self, ctx: ExecutionContext) -> str | None:
        """Return a reason string when a budget limit is exceeded, else None.
        (Deadline/cancellation are handled via ctx.check_limits().)"""
        if ctx.iteration >= self.max_iterations:
            return "max_iterations"
        if self.max_total_tokens is not None:
            total = ctx.usage.input_tokens + ctx.usage.output_tokens
            if total > self.max_total_tokens:
                return "token_budget"
        return None


def deadline_from_now(timeout_seconds: float | None) -> datetime | None:
    if timeout_seconds is None:
        return None
    return datetime.now(UTC) + timedelta(seconds=timeout_seconds)


__all__ = ["LIMIT_MAX_ITERATIONS", "RunLimits", "deadline_from_now"]
