"""Scoring orchestration (S11, ADR 0017 §4–5): run a dataset-snapshot's
scorers over one observation. Pure orchestration — the caller fetched the
rows and persists the verdicts (lazy, persist-once, D49).

Judge resolution is the D28 pattern applied to the read path: the
dataset's `judge_model` resolves through the model factory per scoring
pass, with the requesting principal threading tenant context for stored
credentials. A resolution failure is an honest no-verdict Score — the
result row records it; nothing retries and nothing fabricates.
"""

from __future__ import annotations

from jarvis.domain.auth import Principal
from jarvis.domain.evaluation import EvalCase, EvalDataset, EvalObservation, Score
from jarvis.models.errors import ModelError
from jarvis.ports.model import ModelProviderFactory
from jarvis.scoring.deterministic import build_scorer
from jarvis.scoring.judge import LlmJudgeScorer


async def score_result(
    dataset: EvalDataset,
    case: EvalCase,
    observation: EvalObservation,
    *,
    model_factory: ModelProviderFactory | None = None,
    principal: Principal | None = None,
) -> list[Score]:
    """Every scorer in the dataset's SNAPSHOT runs against this
    observation, in listed order. Scorers never raise (D50) — the list
    always covers every configured scorer."""
    scores: list[Score] = []
    for config in dataset.scorers:
        if config.name == "llm_judge":
            scores.append(await _judge_score(dataset, case, observation, model_factory, principal))
        else:
            scores.append(await build_scorer(config).score(case, observation))
    return scores


async def _judge_score(
    dataset: EvalDataset,
    case: EvalCase,
    observation: EvalObservation,
    model_factory: ModelProviderFactory | None,
    principal: Principal | None,
) -> Score:
    if dataset.judge_model is None or model_factory is None:
        return Score(scorer="llm_judge", passed=None, detail="judge model unavailable")
    try:
        client = await model_factory.resolve(dataset.judge_model, principal=principal)
    except ModelError as exc:
        return Score(
            scorer="llm_judge",
            passed=None,
            detail=f"judge model resolution failed: {exc}",
        )
    return await LlmJudgeScorer(client).score(case, observation)
