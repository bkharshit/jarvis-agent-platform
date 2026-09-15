"""Evaluation domain types (S11, ADR 0017) — pure Pydantic, no IO.

Datasets carry their cases and scorer configs as JSONB snapshots; an
eval run snapshots the dataset (D1) and pins one agent version, so a
past run's meaning is never rewritten by later edits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.domain.agent import ModelRef


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalCase(_Model):
    """One test input. `id` is a stable uuid the results reference —
    cases live inside the dataset's JSONB snapshot (D48), so this id is
    the join key, not a foreign key into a cases table.

    `input` is min_length=1 because it becomes RunRequest.input at run
    creation (min_length=1 there too) — an empty-input case must be
    rejected at the dataset boundary (422), never as a run-time 500
    (found live in the S11 joint session)."""

    id: str
    input: str = Field(min_length=1)
    expected: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)


class ScorerConfig(_Model):
    """A scorer by name + params, listed on the dataset (D50). `llm_judge`
    additionally requires a dataset-level `judge_model` — validated at the
    create/update boundary (422), never at run time."""

    name: Literal["exact", "contains", "regex", "json_schema", "tool_sequence", "llm_judge"]
    params: dict[str, Any] = Field(default_factory=dict)


class EvalDataset(_Model):
    """A named set of cases + scorers (+ optional judge model)."""

    id: str
    name: str = Field(min_length=1)
    description: str = ""
    cases: list[EvalCase] = Field(min_length=1)
    scorers: list[ScorerConfig] = Field(min_length=1)
    judge_model: ModelRef | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset name must not be blank")
        return value


class EvalRun(_Model):
    """One evaluation: dataset snapshot × pinned agent version. `dataset`
    is the snapshot (D1) — the exact input the scorers saw; later dataset
    edits never rewrite it. The row carries NO status — it is derived at
    read from the child run rows (D49); child run_ids live on the
    results."""

    id: str
    dataset_id: str
    agent_id: str
    agent_version_id: str
    dataset: EvalDataset
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Score(_Model):
    """One scorer's verdict on one result. `passed=None` means the scorer
    could not produce a verdict (judge failure — persisted honestly,
    D50); `detail` carries the human explanation either way."""

    scorer: str
    passed: bool | None = None
    score: float | None = None
    detail: str | None = None


class EvalResult(_Model):
    """One persisted child-run score line: the case, the ordinary run it
    produced, and the scores (NULL until lazy scoring persisted them —
    D49). `error` carries a scorer-level failure detail."""

    id: str
    eval_run_id: str
    case_id: str
    run_id: str
    scores: list[Score] | None = None
    error: str | None = None
    scored_at: datetime | None = None


class EvalObservation(_Model):
    """What a scorer sees from a finished child run (D50): terminal
    status, the final message, and the tool-call order. Deliberately
    small — richer fields arrive when a scorer needs them."""

    status: str
    final_message: str | None = None
    tool_names: list[str] = Field(default_factory=list)
