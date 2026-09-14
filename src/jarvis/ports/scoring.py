"""Scorer protocol (S11, ADR 0017 §5, D50) — Protocols ONLY.

A scorer is a named verdict-producer over one case + one observation.
Adapters live in `jarvis.scoring`: five deterministic scorers plus
`llm_judge` (one model call through the dataset's judge model — the D28
resolution pattern; no runtime loop involved).
"""

from __future__ import annotations

from typing import Protocol

from jarvis.domain.evaluation import EvalCase, EvalObservation, Score


class Scorer(Protocol):
    """One scorer. `name` matches the dataset's ScorerConfig name; `score`
    never raises — a scorer that cannot produce a verdict returns
    `passed=None` with the failure in `detail` (persisted honestly)."""

    name: str

    async def score(self, case: EvalCase, observation: EvalObservation) -> Score: ...


__all__ = ["Scorer"]
