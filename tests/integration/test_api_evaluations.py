"""Evaluation routes (S11, ADR 0017 §6): dataset CRUD, eval runs riding the
ordinary queue, lazy persist-once scoring, tenant scoping, comparison.

Turn-ordering discipline: case runs execute CONCURRENTLY (the worker claims
each child as its own task), and the mock provider dispatches scripted
turns from one FIFO. Tests stay order-independent by either matching the
provider's default reply (all cases pass) or deliberately mismatching it
(all fail) — never per-case scripted content on a multi-case dataset."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.domain.evaluation import EvalDataset
from jarvis.models.errors import ModelError

DEFAULT_REPLY = "This is a mock response."  # MockModelProvider.default_content


def _cases_payload(expected: str, count: int = 2) -> list[dict]:
    return [
        {"id": f"c-{i}", "input": f"question {i}?", "expected": expected}
        for i in range(1, count + 1)
    ]


async def _create_dataset(client, *, expected: str, scorers: list[dict] | None = None) -> dict:
    resp = await client.post(
        "/v1/evaluations/datasets",
        json={
            "name": "smoke-dataset",
            "cases": _cases_payload(expected),
            "scorers": scorers or [{"name": "exact"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_run(client, dataset_id: str, agent_id: str) -> dict:
    resp = await client.post(
        f"/v1/evaluations/datasets/{dataset_id}/runs", json={"agent_id": agent_id}
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _wait_completed(client, run_id: str, timeout: float = 30.0) -> dict:
    """Poll the detail route until the derived status leaves `running`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        detail = (await client.get(f"/v1/evaluations/runs/{run_id}")).json()
        if detail["status"] != "running":
            return detail
        assert asyncio.get_running_loop().time() < deadline, f"eval run {run_id} never completed"
        await asyncio.sleep(0.1)


@pytest.mark.db
async def test_dataset_crud_roundtrip(client):
    created = await _create_dataset(client, expected=DEFAULT_REPLY)
    assert created["name"] == "smoke-dataset"
    assert [c["id"] for c in created["cases"]] == ["c-1", "c-2"]
    assert created["scorers"] == [{"name": "exact", "params": {}}]

    listed = (await client.get("/v1/evaluations/datasets")).json()
    assert [d["id"] for d in listed["items"]] == [created["id"]]

    detail = (await client.get(f"/v1/evaluations/datasets/{created['id']}")).json()
    assert detail == created

    patched = await client.patch(
        f"/v1/evaluations/datasets/{created['id']}",
        json={
            "name": "renamed",
            "description": "updated",
            "cases": _cases_payload(DEFAULT_REPLY, count=1),
            "scorers": [{"name": "contains"}],
        },
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "renamed" and body["description"] == "updated"
    assert len(body["cases"]) == 1 and body["scorers"][0]["name"] == "contains"

    deleted = await client.delete(f"/v1/evaluations/datasets/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/evaluations/datasets/{created['id']}")).status_code == 404


@pytest.mark.db
async def test_llm_judge_without_judge_model_422(client):
    resp = await client.post(
        "/v1/evaluations/datasets",
        json={
            "name": "judgeless",
            "cases": _cases_payload(DEFAULT_REPLY, count=1),
            "scorers": [{"name": "llm_judge"}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["kind"] == "validation"

    # the same rule guards PATCH — a valid dataset can't drift into invalid
    created = await _create_dataset(client, expected=DEFAULT_REPLY)
    patched = await client.patch(
        f"/v1/evaluations/datasets/{created['id']}",
        json={
            "name": "drifted",
            "cases": _cases_payload(DEFAULT_REPLY, count=1),
            "scorers": [{"name": "llm_judge"}],
        },
    )
    assert patched.status_code == 422

    # with a judge model present, the dataset is accepted
    ok = await client.post(
        "/v1/evaluations/datasets",
        json={
            "name": "judged",
            "cases": _cases_payload(DEFAULT_REPLY, count=1),
            "scorers": [{"name": "llm_judge"}],
            "judge_model": {"provider": "mock", "model": "judge-1"},
        },
    )
    assert ok.status_code == 201


@pytest.mark.db
async def test_eval_run_queues_children_scores_once(client, container, agent):
    dataset = await _create_dataset(client, expected=DEFAULT_REPLY)
    summary = await _create_run(client, dataset["id"], agent.id)
    assert summary["status"] == "running"
    assert summary["dataset_id"] == dataset["id"]
    assert summary["agent_version_id"]  # pinned — resolved at create time

    detail = await _wait_completed(client, summary["id"])
    assert detail["status"] == "completed"
    assert len(detail["results"]) == 2
    for result in detail["results"]:
        assert result["scores"] == [
            {"scorer": "exact", "passed": True, "score": 1.0, "detail": "matched"}
        ]
        assert result["error"] is None

    # children are ordinary runs: visible in /executions, metadata stamped
    for result in detail["results"]:
        child = (await client.get(f"/v1/executions/{result['run_id']}")).json()
        assert child["run"]["status"] == "succeeded"
        stamp = child["run"]["metadata"]["eval"]
        assert stamp["eval_run_id"] == summary["id"]
        assert stamp["case_id"] == result["case_id"]

    # persist-once (D49): a second read never re-scores — scored_at frozen
    again = (await client.get(f"/v1/evaluations/runs/{summary['id']}")).json()
    first_scored = {r["case_id"]: r["scored_at"] for r in detail["results"]}
    second_scored = {r["case_id"]: r["scored_at"] for r in again["results"]}
    assert second_scored == first_scored
    assert all(ts is not None for ts in second_scored.values())

    # listings carry the derived status and both filters
    listed = (await client.get("/v1/evaluations/runs")).json()
    assert summary["id"] in [r["id"] for r in listed["items"]]
    by_agent = (await client.get(f"/v1/evaluations/runs?agent_id={agent.id}")).json()
    assert summary["id"] in [r["id"] for r in by_agent["items"]]
    empty = (
        await client.get(f"/v1/evaluations/runs?dataset_id={dataset['id']}&agent_id=ghost")
    ).json()
    assert empty["items"] == []


@pytest.mark.db
async def test_failed_child_scored_honestly(client, container, agent, mock):
    """A failed child run is scored honestly (D49): exact fails on a missing
    final message, the run's error rides the result row."""
    from jarvis.models.mock import turn

    mock.add_turn(turn(error=ModelError("mock exploded")))
    # single case: the scripted error turn is consumed by exactly one run
    resp = await client.post(
        "/v1/evaluations/datasets",
        json={
            "name": "single-case",
            "cases": [{"id": "c-1", "input": "boom?", "expected": DEFAULT_REPLY}],
            "scorers": [{"name": "exact"}],
        },
    )
    dataset = resp.json()
    summary = await _create_run(client, dataset["id"], agent.id)
    detail = await _wait_completed(client, summary["id"])
    result = detail["results"][0]
    child = (await client.get(f"/v1/executions/{result['run_id']}")).json()
    assert child["run"]["status"] == "failed"
    assert result["error"] is not None and "mock exploded" in result["error"]
    assert result["scores"][0]["passed"] is False  # no final message to match


@pytest.mark.db
async def test_dataset_with_runs_delete_409(client, container, agent):
    dataset = await _create_dataset(client, expected=DEFAULT_REPLY)
    summary = await _create_run(client, dataset["id"], agent.id)
    await _wait_completed(client, summary["id"])
    resp = await client.delete(f"/v1/evaluations/datasets/{dataset['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["kind"] == "conflict"
    # a dataset without runs deletes fine
    fresh = await _create_dataset(client, expected=DEFAULT_REPLY)
    assert (await client.delete(f"/v1/evaluations/datasets/{fresh['id']}")).status_code == 204


@pytest.mark.db
async def test_tenant_scoping_404(container, client):
    await container.auth.create_tenant("t-eval-other", "Other")
    foreign = await container.evaluations.create_dataset(
        EvalDataset(
            id="ds-foreign",
            name="foreign",
            cases=[{"id": "c-1", "input": "q", "expected": "a"}],
            scorers=[{"name": "exact"}],
        ),
        tenant_id="t-eval-other",
    )
    assert (await client.get(f"/v1/evaluations/datasets/{foreign.id}")).status_code == 404
    listed = (await client.get("/v1/evaluations/datasets")).json()
    assert foreign.id not in [d["id"] for d in listed["items"]]
    assert (
        await client.post(
            f"/v1/evaluations/datasets/{foreign.id}/runs", json={"agent_id": "whatever"}
        )
    ).status_code == 404
    assert (await client.delete(f"/v1/evaluations/datasets/{foreign.id}")).status_code == 404


@pytest.mark.db
async def test_compare_aggregates_by_version(client, container, agent):
    passing = await _create_dataset(client, expected=DEFAULT_REPLY)
    run_v1 = await _create_run(client, passing["id"], agent.id)
    await _wait_completed(client, run_v1["id"])

    # publish version 2, then a dataset whose expectation can never match
    await container.agents.update_and_publish(agent, label="v2")
    failing = await _create_dataset(client, expected="never the mock reply")
    run_v2 = await _create_run(client, failing["id"], agent.id)
    detail = await _wait_completed(client, run_v2["id"])
    assert detail["agent_version_id"] != run_v1["agent_version_id"]

    resp = await client.get(f"/v1/evaluations/compare?agent_id={agent.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == agent.id
    by_version = {v["agent_version_id"]: v for v in body["versions"]}
    assert set(by_version) == {run_v1["agent_version_id"], run_v2["agent_version_id"]}

    v1 = by_version[run_v1["agent_version_id"]]
    assert v1["runs"] == 1 and v1["cases"] == 2 and v1["scored"] == 2 and v1["passed"] == 2
    assert v1["pass_rate"] == 1.0
    assert v1["scorers"] == [{"scorer": "exact", "scored": 2, "passed": 2, "mean": 1.0}]

    v2 = by_version[run_v2["agent_version_id"]]
    assert v2["cases"] == 2 and v2["scored"] == 2 and v2["passed"] == 0
    assert v2["pass_rate"] == 0.0
    assert v2["scorers"][0]["mean"] == 0.0
