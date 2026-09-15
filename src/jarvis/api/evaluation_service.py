"""Eval-run creation (S11, ADR 0017 §3, D48/D50): route-level composition
over the EXISTING queue machinery — no new worker role, no new queue
contract. Each case becomes an ordinary agent-kind run (the same
`queue_message` + `create_queued_run` the manual run route uses), pinned
to the agent's latest published version, with the eval linkage stamped in
metadata for traceability. The eval-run row + results persist AFTER the
children (D48 ordering): every child run row exists by the time the
eval_results FKs are written.

The dataset-scorer validation (llm_judge ⇒ judge_model) also lives here —
shared by the dataset create/update boundary (422) and the run-create
path, so an invalid dataset can never be snapshotted or executed.
"""

from __future__ import annotations

from uuid import uuid4

from jarvis.api.errors import ApiError
from jarvis.config import Settings
from jarvis.domain.auth import Principal
from jarvis.domain.evaluation import EvalCase, EvalDataset, EvalRun
from jarvis.ports.repository import AgentRepo, EvalRepo
from jarvis.runtime.child_runs import QueuedRunWriter, child_message, enqueue_child_run
from jarvis.runtime.limits import deadline_from_now


def validate_dataset_scorers(dataset: EvalDataset) -> None:
    """D50: `llm_judge` in scorers requires a dataset-level judge_model —
    422 at the boundary, never a snapshotted-invalid dataset."""
    if any(s.name == "llm_judge" for s in dataset.scorers) and dataset.judge_model is None:
        raise ApiError(
            422,
            "validation",
            "scorer 'llm_judge' requires judge_model on the dataset",
            details={
                "errors": [
                    {
                        "loc": ["body", "judge_model"],
                        "msg": "required when scorers include llm_judge",
                        "type": "missing",
                    }
                ]
            },
        )


async def create_eval_run(
    *,
    settings: Settings,
    agents: AgentRepo,
    evals: EvalRepo,
    executions: QueuedRunWriter,
    dataset: EvalDataset,
    agent_id: str,
    principal: Principal,
) -> EvalRun:
    """Pin the version, enqueue one ordinary run per case, then persist the
    eval run + results in one transaction. The children carry
    `session_id=None` — each case is an isolated run with no conversation —
    and metadata {"eval": {eval_run_id, case_id}} for traceability in
    /executions."""
    validate_dataset_scorers(dataset)
    version = await agents.latest_version(agent_id)
    if version is None:
        raise ApiError(404, "not_found", f"agent {agent_id!r} has no published version")
    eval_run_id = str(uuid4())
    children: list[tuple[EvalCase, str]] = []
    for case in dataset.cases:
        # The shared child-run composition (S14 D52): a fresh trace + the
        # platform deadline per case — byte-identical to the former
        # queue_message composition here, no parent linkage.
        message = child_message(
            agent_id=agent_id,
            agent_version_id=version.id,
            input=case.input,
            tenant_id=principal.tenant_id,
            principal=principal,
            trace_id=str(uuid4()),
            deadline=deadline_from_now(settings.run_timeout_seconds),
            metadata={"eval": {"eval_run_id": eval_run_id, "case_id": case.id}},
            variables=dict(case.variables),
        )
        await enqueue_child_run(executions=executions, message=message)
        children.append((case, message.run_id))
    return await evals.create_run(
        dataset,
        agent_id,
        version.id,
        children,
        run_id=eval_run_id,
        tenant_id=principal.tenant_id,
    )


__all__ = ["create_eval_run", "validate_dataset_scorers"]
