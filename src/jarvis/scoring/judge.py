"""The LLM-as-judge scorer (S11, ADR 0017 §5, D50).

ONE non-streaming `generate()` through a client the caller resolved from
the dataset's `judge_model` (the D28 pattern — no runtime loop, no tool
surface, no second model machinery). The call happens on the read path,
outside any run: its usage is not accounted to any run budget, and a
failure is an honest no-verdict Score — never a retry, never a
fabricated pass.
"""

from __future__ import annotations

import re

from jarvis.domain.evaluation import EvalCase, EvalObservation, Score
from jarvis.domain.message import Message
from jarvis.models.errors import ModelError
from jarvis.models.types import ModelRequest
from jarvis.ports.model import ModelClient
from jarvis.ports.scoring import Scorer

_JUDGE_SYSTEM_PROMPT = (
    "You are an evaluation judge. Compare the agent's final answer against "
    "the expected answer for the test case, judging correctness of the "
    "answer itself (not style). Reply in EXACTLY this format:\n"
    "VERDICT: PASS\nREASON: <one sentence>\n"
    "or\nVERDICT: FAIL\nREASON: <one sentence>"
)


class LlmJudgeScorer(Scorer):
    name = "llm_judge"

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    async def score(self, case: EvalCase, observation: EvalObservation) -> Score:
        try:
            return await self._judge(case, observation)
        except ModelError as exc:
            return Score(
                scorer=self.name,
                passed=None,
                detail=f"judge model error: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — the honesty boundary itself
            return Score(
                scorer=self.name,
                passed=None,
                detail=f"judge error: {type(exc).__name__}: {exc}",
            )

    async def _judge(self, case: EvalCase, observation: EvalObservation) -> Score:
        user = (
            f"Input:\n{case.input}\n\n"
            f"Expected answer:\n{case.expected or '(none given)'}\n\n"
            f"Agent's final answer:\n{observation.final_message or '(none)'}"
        )
        request = ModelRequest(
            model=self._client.ref.model,
            messages=[
                Message(role="system", content=_JUDGE_SYSTEM_PROMPT),
                Message(role="user", content=user),
            ],
            temperature=0.0,
        )
        response = await self._client.generate(request)
        return _parse_verdict(response.message.text)


def _parse_verdict(text: str) -> Score:
    """The contract is a `VERDICT: PASS|FAIL` line; anything else is an
    honest no-verdict (the judge said nothing usable)."""
    match = re.search(r"VERDICT:\s*(PASS|FAIL)", text, re.IGNORECASE)
    if match is None:
        return Score(
            scorer="llm_judge",
            passed=None,
            detail=f"judge reply had no VERDICT line: {text[:200]!r}",
        )
    passed = match.group(1).upper() == "PASS"
    reason_match = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    detail = reason_match.group(1).strip() if reason_match else text.strip()
    return Score(
        scorer="llm_judge",
        passed=passed,
        score=1.0 if passed else 0.0,
        detail=detail,
    )
