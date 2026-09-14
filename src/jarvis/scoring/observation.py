"""Observation building (S11, ADR 0017 §4): a finished child run plus its
persisted tool executions become the small `EvalObservation` a scorer
sees. Pure mapping — no IO; the caller fetches the rows."""

from __future__ import annotations

from jarvis.domain.evaluation import EvalObservation
from jarvis.domain.execution import RunResult
from jarvis.domain.tools import ToolResult


def build_observation(run: RunResult, tool_executions: list[ToolResult]) -> EvalObservation:
    """The run row carries the terminal status and final message; the
    tool-execution rows (in call order) give the sequence the
    tool_sequence scorer compares against."""
    return EvalObservation(
        status=run.status,
        final_message=run.final_message,
        tool_names=[result.tool_name for result in tool_executions],
    )
