"""Deterministic scorers (S11, ADR 0017 §5, D50).

A scorer produces a verdict over one case + one observation; it never
raises — a crash is a persisted `passed=None` with the failure in
`detail` (the read path has no run to fail, D5 spirit). All five
adapters here are deterministic: no model, no network, no clock — the
unit suite pins them without a DB.
"""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

from jarvis.domain.evaluation import EvalCase, EvalObservation, Score
from jarvis.ports.scoring import Scorer


class _DeterministicScorer:
    """Shared shell: run the pure `_judge` and translate any crash into
    an honest no-verdict Score (never an exception past the port)."""

    name: str

    async def score(self, case: EvalCase, observation: EvalObservation) -> Score:
        try:
            return self._judge(case, observation)
        except Exception as exc:  # noqa: BLE001 — the honesty boundary itself
            return Score(
                scorer=self.name,
                passed=None,
                detail=f"scorer error: {type(exc).__name__}: {exc}",
            )

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:  # pragma: no cover
        raise NotImplementedError

    def _expected(self, case: EvalCase) -> str | None:
        if case.expected is None:
            return None
        return case.expected

    def _no_expected(self) -> Score:
        return Score(scorer=self.name, passed=None, detail="case has no expected value")

    def _verdict(self, passed: bool, detail: str) -> Score:
        return Score(
            scorer=self.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            detail=detail,
        )


class ExactScorer(_DeterministicScorer):
    """The final message equals the expected text (whitespace-trimmed)."""

    name = "exact"

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        expected = self._expected(case)
        if expected is None:
            return self._no_expected()
        actual = (observation.final_message or "").strip()
        passed = actual == expected.strip()
        return self._verdict(
            passed,
            "matched" if passed else f"expected {expected.strip()!r}, got {actual!r}",
        )


class ContainsScorer(_DeterministicScorer):
    """The expected text appears anywhere in the final message."""

    name = "contains"

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        expected = self._expected(case)
        if expected is None:
            return self._no_expected()
        actual = observation.final_message or ""
        passed = expected in actual
        return self._verdict(
            passed,
            "found" if passed else f"{expected!r} not found in the final message",
        )


class RegexScorer(_DeterministicScorer):
    """`params["pattern"]` matches the final message (re.search, DOTALL —
    model replies wrap across lines)."""

    name = "regex"

    def __init__(self, params: dict[str, Any]) -> None:
        self._pattern: Any = params.get("pattern")

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        if not isinstance(self._pattern, str):
            return Score(scorer=self.name, passed=None, detail="scorer needs params.pattern")
        actual = observation.final_message or ""
        passed = re.search(self._pattern, actual, re.DOTALL) is not None
        return self._verdict(
            passed,
            f"pattern {self._pattern!r} {'matched' if passed else 'did not match'}",
        )


class JsonSchemaScorer(_DeterministicScorer):
    """The final message parses as JSON and validates against
    `params["schema"]` — the same jsonschema seam the runtime's
    structured-output repair uses."""

    name = "json_schema"

    def __init__(self, params: dict[str, Any]) -> None:
        self._schema: Any = params.get("schema")

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        if not isinstance(self._schema, dict):
            return Score(scorer=self.name, passed=None, detail="scorer needs params.schema")
        actual = observation.final_message or ""
        try:
            payload = json.loads(actual)
        except json.JSONDecodeError as exc:
            return self._verdict(False, f"reply is not valid JSON: {exc.msg}")
        try:
            jsonschema.validate(payload, self._schema)
        except jsonschema.ValidationError as exc:
            return self._verdict(False, exc.message)
        return self._verdict(True, "valid against the schema")


class ToolSequenceScorer(_DeterministicScorer):
    """`params["expected"]` — the ordered tool-name list the run should
    have produced, compared name-wise against the observed order."""

    name = "tool_sequence"

    def __init__(self, params: dict[str, Any]) -> None:
        self._expected: Any = params.get("expected")

    def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        if not isinstance(self._expected, list):
            return Score(scorer=self.name, passed=None, detail="scorer needs params.expected")
        actual = observation.tool_names
        passed = actual == self._expected
        return self._verdict(
            passed,
            ("tool order matched" if passed else f"expected {self._expected!r}, got {actual!r}"),
        )


def build_scorer(config: Any) -> Scorer:
    """The ScorerConfig → adapter mapping. `llm_judge` registers when its
    model-backed adapter lands (the dataset's judge_model resolves
    through the model factory — not a deterministic scorer)."""
    name = config.name
    scorer: Scorer
    if name == "exact":
        scorer = ExactScorer()
    elif name == "contains":
        scorer = ContainsScorer()
    elif name == "regex":
        scorer = RegexScorer(config.params)
    elif name == "json_schema":
        scorer = JsonSchemaScorer(config.params)
    elif name == "tool_sequence":
        scorer = ToolSequenceScorer(config.params)
    else:
        raise ValueError(f"scorer {name!r} is not registered")
    return scorer
