"""Deterministic scorers + observation builder (S11, ADR 0017 §5, D50)."""

import asyncio

import pytest
from pydantic import ValidationError

from jarvis.domain.evaluation import EvalCase, EvalObservation, ScorerConfig
from jarvis.domain.execution import RunResult
from jarvis.domain.tools import ToolResult
from jarvis.scoring import build_observation, build_scorer


def _case(expected: str | None = "4") -> EvalCase:
    return EvalCase(id="c-1", input="what is 2+2?", expected=expected)


def _obs(
    status: str = "succeeded",
    final_message: str | None = "4",
    tool_names: list[str] | None = None,
) -> EvalObservation:
    return EvalObservation(status=status, final_message=final_message, tool_names=tool_names or [])


def _score(config: dict, case: EvalCase, obs: EvalObservation):
    scorer = build_scorer(ScorerConfig.model_validate(config))
    return asyncio.run(scorer.score(case, obs))


class TestExact:
    def test_match(self) -> None:
        s = _score({"name": "exact"}, _case("4"), _obs(final_message=" 4\n"))
        assert s.passed is True and s.score == 1.0

    def test_mismatch_details_both_sides(self) -> None:
        s = _score({"name": "exact"}, _case("4"), _obs(final_message="The answer is 4"))
        assert s.passed is False
        assert "The answer is 4" in (s.detail or "")

    def test_missing_expected_is_no_verdict(self) -> None:
        s = _score({"name": "exact"}, _case(None), _obs())
        assert s.passed is None and "expected" in (s.detail or "")

    def test_no_final_message_fails(self) -> None:
        s = _score({"name": "exact"}, _case("4"), _obs(final_message=None))
        assert s.passed is False


class TestContains:
    def test_found_and_missing(self) -> None:
        assert _score(
            {"name": "contains"}, _case("Paris"), _obs(final_message="Visit Paris first.")
        ).passed
        assert not _score(
            {"name": "contains"},
            _case("Lyon"),
            _obs(final_message="Visit Paris first."),
        ).passed

    def test_multiline_reply_still_searched(self) -> None:
        obs = _obs(final_message="Here is the plan:\n1. deploy\n2. verify")
        assert _score({"name": "contains"}, _case("2. verify"), obs).passed


class TestRegex:
    def test_dotall_multiline(self) -> None:
        obs = _obs(final_message="answer:\n42")
        s = _score({"name": "regex", "params": {"pattern": r"answer:\n\d+"}}, _case(), obs)
        assert s.passed is True

    def test_missing_pattern_is_no_verdict(self) -> None:
        s = _score({"name": "regex"}, _case(), _obs())
        assert s.passed is None and "pattern" in (s.detail or "")

    def test_crash_is_an_honest_no_verdict(self) -> None:
        # an uncompilable pattern raises inside re — the shell catches it
        s = _score({"name": "regex", "params": {"pattern": "(("}}, _case(), _obs())
        assert s.passed is None and "scorer error" in (s.detail or "")


class TestJsonSchema:
    SCHEMA = {
        "type": "object",
        "properties": {"answer": {"type": "number"}},
        "required": ["answer"],
    }

    def test_valid(self) -> None:
        s = _score(
            {"name": "json_schema", "params": {"schema": self.SCHEMA}},
            _case(),
            _obs(final_message='{"answer": 4}'),
        )
        assert s.passed is True

    def test_invalid_json_fails_with_detail(self) -> None:
        s = _score(
            {"name": "json_schema", "params": {"schema": self.SCHEMA}},
            _case(),
            _obs(final_message="{oops"),
        )
        assert s.passed is False and "not valid JSON" in (s.detail or "")

    def test_schema_violation_fails(self) -> None:
        s = _score(
            {"name": "json_schema", "params": {"schema": self.SCHEMA}},
            _case(),
            _obs(final_message='{"answer": "four"}'),
        )
        assert s.passed is False and s.detail

    def test_missing_schema_is_no_verdict(self) -> None:
        s = _score({"name": "json_schema"}, _case(), _obs())
        assert s.passed is None


class TestToolSequence:
    def test_order_matters(self) -> None:
        obs = _obs(tool_names=["calculator", "current_time"])
        assert _score(
            {"name": "tool_sequence", "params": {"expected": ["calculator", "current_time"]}},
            _case(),
            obs,
        ).passed
        assert not _score(
            {"name": "tool_sequence", "params": {"expected": ["current_time", "calculator"]}},
            _case(),
            obs,
        ).passed

    def test_missing_expected_is_no_verdict(self) -> None:
        s = _score({"name": "tool_sequence"}, _case(), _obs())
        assert s.passed is None

    def test_erroring_tool_calls_still_count(self) -> None:
        # is_error is not part of the sequence — the call happened
        assert _score(
            {"name": "tool_sequence", "params": {"expected": ["memory_get"]}},
            _case(),
            _obs(tool_names=["memory_get"]),
        ).passed


class TestBuildScorerAndObservation:
    def test_unknown_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScorerConfig.model_validate({"name": "vibes"})

    def test_observation_maps_the_run(self) -> None:
        run = RunResult(
            run_id="run-1",
            agent_id="a-1",
            status="succeeded",
            input="q",
            agent_version_id="v",
            final_message="4",
        )
        tools = [
            ToolResult(tool_call_id="c1", tool_name="calculator", output="4"),
            ToolResult(tool_call_id="c2", tool_name="current_time", output="…", is_error=True),
        ]
        obs = build_observation(run, tools)
        assert obs.status == "succeeded"
        assert obs.final_message == "4"
        assert obs.tool_names == ["calculator", "current_time"]  # order, errors included
