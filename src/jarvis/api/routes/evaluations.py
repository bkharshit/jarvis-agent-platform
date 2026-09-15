"""Evaluation routes (S11, ADR 0017 §6, D48–D50): datasets CRUD, eval-run
create/list/detail, and version comparison — top-level `/v1/evaluations*`
(the McpServerRepo first-class-resource pattern, not nested under agents).

The create route composes `create_eval_run` (api.evaluation_service) over
the UNCHANGED queue machinery: each case becomes an ordinary agent-kind
run. Run status is DERIVED from the child rows at read time (D49 — no
status column), and scoring runs lazily on the first completed detail
read, persisted once via `save_scores` (the scores-IS-NULL guard is the
idempotency barrier: a later read never re-scores; re-evaluation is a new
eval run)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer
from jarvis.api.errors import ApiError
from jarvis.api.evaluation_service import create_eval_run, validate_dataset_scorers
from jarvis.api.routes._run_routes import ContainerDep
from jarvis.api.schemas import (
    EvalCompareResponse,
    EvalDatasetList,
    EvalDatasetUpsert,
    EvalRunCreate,
    EvalRunDetail,
    EvalRunList,
    EvalRunSummary,
    EvalScorerStats,
    EvalVersionCompare,
)
from jarvis.domain.evaluation import EvalDataset, EvalResult, EvalRun
from jarvis.scoring.observation import build_observation
from jarvis.scoring.runner import score_result

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

__all__ = ["router"]

# ADR 0004 terminal statuses — a child in any of these is done; everything
# else (queued/running/awaiting_input) keeps the eval run honest at
# "running". A missing child row reads as non-terminal too.
_TERMINAL_STATUSES: set[str] = {"succeeded", "failed", "cancelled", "timed_out"}


async def _visible_dataset(
    auth: AuthContext, container: AppContainer, dataset_id: str
) -> EvalDataset:
    dataset = await container.evaluations.get_dataset(
        dataset_id, tenant_id=auth.principal.tenant_id
    )
    if dataset is None:
        raise ApiError(404, "not_found", f"eval dataset {dataset_id!r} not found")
    return dataset


async def _visible_run(auth: AuthContext, container: AppContainer, run_id: str) -> EvalRun:
    run = await container.evaluations.get_run(run_id, tenant_id=auth.principal.tenant_id)
    if run is None:
        raise ApiError(404, "not_found", f"eval run {run_id!r} not found")
    return run


async def _derived_status(auth: AuthContext, results: list[EvalResult]) -> str:
    """D49: no status column — every child terminal ⇒ completed, any
    child missing or non-terminal ⇒ running."""
    for result in results:
        child = await auth.executions.get(result.run_id)
        if child is None or child.status not in _TERMINAL_STATUSES:
            return "running"
    return "completed"


async def _score_pending(
    auth: AuthContext, container: AppContainer, run: EvalRun, results: list[EvalResult]
) -> None:
    """Lazy scoring (D49): for each still-unscored result, load the child
    run + tool order into an observation, run the SNAPSHOT's scorers, and
    persist once. The repo's scores-IS-NULL guard makes a concurrent or
    repeated read a no-op. A failed child is scored honestly — its error
    rides the result row, the status rides the observation."""
    dataset = run.dataset
    cases = {case.id: case for case in dataset.cases}
    for result in results:
        if result.scores is not None:
            continue
        child = await auth.executions.get(result.run_id)
        if child is None or child.status not in _TERMINAL_STATUSES:
            continue  # cannot happen at derived "completed"; stay honest
        case = cases.get(result.case_id)
        if case is None:
            continue
        observation = build_observation(
            child, await auth.executions.list_tool_executions(result.run_id)
        )
        scores = await score_result(
            dataset,
            case,
            observation,
            model_factory=container.models,
            principal=auth.principal,
        )
        scored_at = datetime.now(UTC)
        await container.evaluations.save_scores(
            run.id,
            result.case_id,
            scores,
            child.error,
            scored_at=scored_at,
            tenant_id=auth.principal.tenant_id,
        )
        result.scores = scores
        result.error = child.error
        result.scored_at = scored_at  # the SAME clock the repo persisted


# --- datasets -------------------------------------------------------------


@router.get("/datasets")
async def list_datasets(
    auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> EvalDatasetList:
    datasets = await container.evaluations.list_datasets(tenant_id=auth.principal.tenant_id)
    return EvalDatasetList(items=datasets)


@router.post("/datasets", status_code=201)
async def create_dataset(
    req: EvalDatasetUpsert, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> EvalDataset:
    dataset = EvalDataset(
        id=str(uuid4()),
        name=req.name,
        description=req.description,
        cases=req.cases,
        scorers=req.scorers,
        judge_model=req.judge_model,
    )
    validate_dataset_scorers(dataset)  # 422: llm_judge requires judge_model (D50)
    return await container.evaluations.create_dataset(dataset, tenant_id=auth.principal.tenant_id)


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> EvalDataset:
    return await _visible_dataset(auth, container, dataset_id)


@router.patch("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    req: EvalDatasetUpsert,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> EvalDataset:
    current = await _visible_dataset(auth, container, dataset_id)
    updated = current.model_copy(
        update={
            "name": req.name,
            "description": req.description,
            "cases": req.cases,
            "scorers": req.scorers,
            "judge_model": req.judge_model,
        }
    )
    validate_dataset_scorers(updated)  # 422 at the boundary, before the write
    result = await container.evaluations.update_dataset(updated, tenant_id=auth.principal.tenant_id)
    if result is None:
        raise ApiError(404, "not_found", f"eval dataset {dataset_id!r} not found")
    return result


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> None:
    await _visible_dataset(auth, container, dataset_id)  # absent/foreign → 404
    deleted = await container.evaluations.delete_dataset(
        dataset_id, tenant_id=auth.principal.tenant_id
    )
    if not deleted:
        # exists but referenced by eval runs — re-evaluation is a new run,
        # deletion would orphan the snapshots' meaning
        raise ApiError(
            409, "conflict", f"eval dataset {dataset_id!r} has eval runs and cannot be deleted"
        )


# --- eval runs ------------------------------------------------------------


@router.post("/datasets/{dataset_id}/runs", status_code=202)
async def create_run(
    dataset_id: str,
    req: EvalRunCreate,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> EvalRunSummary:
    dataset = await _visible_dataset(auth, container, dataset_id)
    run = await create_eval_run(
        settings=container.settings,
        agents=auth.agents,
        evals=container.evaluations,
        executions=auth.executions,
        dataset=dataset,
        agent_id=req.agent_id,
        principal=auth.principal,
    )
    # every child is queued at this instant — the derived status is
    # "running" by construction (202: the runs happen after the response)
    return EvalRunSummary(
        id=run.id,
        dataset_id=run.dataset_id,
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        status="running",
        created_at=run.created_at,
    )


@router.get("/runs")
async def list_runs(
    agent_id: str | None = None,
    dataset_id: str | None = None,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> EvalRunList:
    runs = await container.evaluations.list_runs(
        agent_id=agent_id, dataset_id=dataset_id, tenant_id=auth.principal.tenant_id
    )
    summaries: list[EvalRunSummary] = []
    for run in runs:
        results = await container.evaluations.get_results(
            run.id, tenant_id=auth.principal.tenant_id
        )
        summaries.append(
            EvalRunSummary(
                id=run.id,
                dataset_id=run.dataset_id,
                agent_id=run.agent_id,
                agent_version_id=run.agent_version_id,
                status=await _derived_status(auth, results),
                created_at=run.created_at,
            )
        )
    return EvalRunList(items=summaries)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> EvalRunDetail:
    run = await _visible_run(auth, container, run_id)
    results = await container.evaluations.get_results(run.id, tenant_id=auth.principal.tenant_id)
    # results come back case_id-ordered; present them in the dataset
    # snapshot's case order — the order the runs were enqueued in
    snapshot_order = {case.id: i for i, case in enumerate(run.dataset.cases)}
    results.sort(key=lambda r: snapshot_order.get(r.case_id, len(snapshot_order)))
    status = await _derived_status(auth, results)
    if status == "completed":
        await _score_pending(auth, container, run, results)  # lazy, persist-once (D49)
    return EvalRunDetail(
        id=run.id,
        dataset_id=run.dataset_id,
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        status=status,
        created_at=run.created_at,
        dataset=run.dataset,
        results=results,
    )


# --- comparison -----------------------------------------------------------


@router.get("/compare")
async def compare(
    agent_id: str,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> EvalCompareResponse:
    grouped = await container.evaluations.list_version_scores(
        agent_id, tenant_id=auth.principal.tenant_id
    )
    versions: list[EvalVersionCompare] = []
    by_version: dict[str, list[EvalResult]] = {}
    for run, results in grouped:
        by_version.setdefault(run.agent_version_id, []).extend(results)
    for agent_version_id, results in by_version.items():
        scored_cases = [
            r
            for r in results
            if r.scores is not None and all(s.passed is not None for s in r.scores)
        ]
        passed_cases = [
            r for r in scored_cases if r.scores is not None and all(s.passed for s in r.scores)
        ]
        stats: dict[str, dict[str, list[float]]] = {}
        for result in results:
            for score in result.scores or []:
                bucket = stats.setdefault(score.scorer, {"verdicts": [], "values": []})
                if score.passed is not None:
                    bucket["verdicts"].append(1.0 if score.passed else 0.0)
                if score.score is not None:
                    bucket["values"].append(score.score)
        versions.append(
            EvalVersionCompare(
                agent_version_id=agent_version_id,
                runs=len({r.eval_run_id for r in results}),
                cases=len(results),
                scored=len(scored_cases),
                passed=len(passed_cases),
                pass_rate=(len(passed_cases) / len(scored_cases) if scored_cases else None),
                scorers=[
                    EvalScorerStats(
                        scorer=name,
                        scored=len(bucket["verdicts"]),
                        passed=int(sum(bucket["verdicts"])),
                        mean=(
                            sum(bucket["values"]) / len(bucket["values"])
                            if bucket["values"]
                            else None
                        ),
                    )
                    for name, bucket in stats.items()
                ],
            )
        )
    return EvalCompareResponse(agent_id=agent_id, versions=versions)
