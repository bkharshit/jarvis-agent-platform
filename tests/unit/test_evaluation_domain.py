"""Domain shapes for the evaluation framework (S11, ADR 0017)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import ModelRef
from jarvis.domain.evaluation import (
    EvalCase,
    EvalDataset,
    EvalObservation,
    EvalResult,
    EvalRun,
    Score,
    ScorerConfig,
)


def _dataset(**overrides: object) -> EvalDataset:
    base: dict[str, object] = {
        "id": "ds-1",
        "name": "smoke",
        "cases": [{"id": "c-1", "input": "what is 2+2?", "expected": "4"}],
        "scorers": [{"name": "exact"}],
    }
    base.update(overrides)
    return EvalDataset.model_validate(base)


class TestEvalCase:
    def test_expected_and_variables_optional(self) -> None:
        case = EvalCase(id="c-1", input="hello")
        assert case.expected is None
        assert case.variables == {}

    def test_variables_are_strings(self) -> None:
        with pytest.raises(ValidationError):
            EvalCase(id="c-1", input="hi", variables={"n": 3})  # type: ignore[dict-item]


class TestScorerConfig:
    def test_unknown_scorer_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScorerConfig(name="vibes")

    def test_every_documented_name_parses(self) -> None:
        for name in ("exact", "contains", "regex", "json_schema", "tool_sequence", "llm_judge"):
            assert ScorerConfig(name=name).name == name

    def test_params_default_empty(self) -> None:
        assert ScorerConfig(name="regex", params={"pattern": "x"}).params == {"pattern": "x"}


class TestEvalDataset:
    def test_minimal_parses_with_defaults(self) -> None:
        ds = _dataset()
        assert ds.description == ""
        assert ds.judge_model is None
        assert ds.cases[0].id == "c-1"

    def test_judge_model_is_a_model_ref(self) -> None:
        ds = _dataset(judge_model={"provider": "mock", "model": "mock-1"})
        assert ds.judge_model == ModelRef(provider="mock", model="mock-1")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            _dataset(weighting="fancy")

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _dataset(name="   ")

    def test_at_least_one_case_and_scorer(self) -> None:
        with pytest.raises(ValidationError):
            _dataset(cases=[])
        with pytest.raises(ValidationError):
            _dataset(scorers=[])


class TestEvalRunAndResults:
    def test_run_carries_the_snapshot_and_no_status(self) -> None:
        # D1: the dataset snapshot rides the run (the exact input the
        # scorers saw). D49: status is derived at read from the child
        # rows, never stored.
        run = EvalRun(
            id="er-1",
            dataset_id="ds-1",
            agent_id="a-1",
            agent_version_id="ver-1",
            dataset=_dataset(),
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        assert run.dataset.name == "smoke"
        assert not run.model_fields.keys() & {"status", "scores", "error"}

    def test_result_scores_nullable_until_scored(self) -> None:
        result = EvalResult(id="r-1", eval_run_id="er-1", case_id="c-1", run_id="run-1")
        assert result.scores is None
        assert result.scored_at is None
        scored = result.model_copy(
            update={
                "scores": [Score(scorer="exact", passed=True, score=1.0, detail="match")],
                "scored_at": datetime.now(UTC),
            }
        )
        assert scored.scores is not None and scored.scores[0].passed is True

    def test_score_passed_is_tristate(self) -> None:
        # D50: judge failure persists passed=None + detail — never fabricated.
        failed = Score(scorer="llm_judge", passed=None, detail="judge model unreachable")
        assert failed.passed is None
        assert Score(scorer="exact").passed is None  # absent == not produced


class TestEvalObservation:
    def test_shape(self) -> None:
        obs = EvalObservation(status="succeeded", final_message="4", tool_names=["calculator"])
        assert obs.tool_names == ["calculator"]
        assert EvalObservation(status="failed").final_message is None
        assert EvalObservation(status="failed").tool_names == []
