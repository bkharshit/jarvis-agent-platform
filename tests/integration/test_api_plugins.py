"""S3 integration — the real chain: installed distribution → entry points →
allow-list → registry → API → runtime.

Uses the fixture distribution (installed by `uv sync --extra dev`): an
allow-listed `plan_execute` agent is created and run through the API with
zero core changes; a de-allow-listed create 422s while the pinned version
still runs — to a persisted `strategy` failure."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from jarvis.api.app import create_app
from jarvis.api.deps import AppContainer
from jarvis.config import Settings
from jarvis.models.mock import MockModelProvider

pytestmark = pytest.mark.db

CREATE_BODY = {
    "name": "plugin-agent",
    "model": {"provider": "mock", "model": "mock-model"},
    "strategy": {"type": "plan_execute", "params": {}},
}

ALLOWLIST = "JARVIS_STRATEGY_PLUGIN_ALLOWLIST"


async def _truncate(container: AppContainer) -> None:
    from tests.integration.conftest import _truncate as conftest_truncate

    await conftest_truncate(container)


@pytest_asyncio.fixture
async def plugin_container(mock: MockModelProvider, monkeypatch) -> AppContainer:
    monkeypatch.setenv(ALLOWLIST, "plan_execute,raise_plugin")
    container = AppContainer.from_settings(Settings(), mock_provider=mock)
    await _truncate(container)
    await container.start_worker()
    yield container
    await container.aclose()


@pytest_asyncio.fixture
async def plugin_client(plugin_container: AppContainer):
    app = create_app(plugin_container.settings)
    app.state.container = plugin_container
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _terminal_event(client: httpx.AsyncClient, run_id: str) -> dict[str, object]:
    events = (await client.get(f"/v1/executions/{run_id}/events")).json()
    terminals = [
        e
        for e in events["events"]
        if e["event"]["type"] in ("run.completed", "run.failed", "run.cancelled")
    ]
    assert len(terminals) == 1, "exactly one terminal event (D5)"
    return terminals[0]["event"]


@pytest.mark.db
async def test_capabilities_lists_plugin_strategies(plugin_client):
    resp = await plugin_client.get("/v1/capabilities")
    assert resp.status_code == 200
    plugins = resp.json()["sections"]["plugins"]
    assert plugins["enabled"] is True
    strategies = {s["name"]: s for s in plugins["detail"]["strategies"]}
    assert strategies["function_calling"]["origin"] == "builtin"
    assert strategies["plan_execute"]["origin"] == "plugin"
    assert strategies["plan_execute"]["distribution"] == "jarvis-strategy-fixtures"
    assert plugins["detail"]["allowlist"] == ["plan_execute", "raise_plugin"]


@pytest.mark.db
async def test_plan_execute_agent_runs_end_to_end(plugin_client, mock):
    from jarvis.models.mock import turn

    resp = await plugin_client.post("/v1/agents", json=CREATE_BODY)
    assert resp.status_code == 201
    agent_id = resp.json()["definition"]["id"]

    mock.add_turn(turn("PLAN:\n1. add two numbers"))  # plan think step
    mock.add_turn(turn("STEP 1: 1 + 1 = 2"))  # execute think step
    mock.add_turn(turn("DONE: all steps executed"))  # terminal reply
    resp = await plugin_client.post(f"/v1/agents/{agent_id}/run", json={"input": "add"})
    assert resp.status_code == 200
    result = resp.json()
    assert result["status"] == "succeeded"
    assert result["final_message"].startswith("DONE:")

    replay = (await plugin_client.get(f"/v1/executions/{result['run_id']}/events")).json()
    types = [e["event"]["type"] for e in replay["events"]]
    # three think/finish phases, each one model invocation, one terminal
    assert types.count("model.invocation.started") == 3
    assert types.count("run.completed") == 1
    assert "run.failed" not in types


@pytest.mark.db
async def test_raising_plugin_is_persisted_terminal_strategy_failure(plugin_client):
    resp = await plugin_client.post(
        "/v1/agents",
        json={**CREATE_BODY, "name": "broken-agent", "strategy": {"type": "raise_plugin"}},
    )
    agent_id = resp.json()["definition"]["id"]

    resp = await plugin_client.post(f"/v1/agents/{agent_id}/run", json={"input": "go"})
    assert resp.status_code == 200  # the route never 500s (D5)
    run_id = resp.json()["run_id"]
    assert resp.json()["status"] == "failed"
    detail = (await plugin_client.get(f"/v1/executions/{run_id}")).json()
    assert detail["run"]["status"] == "failed"  # persisted, worker acked
    terminal = await _terminal_event(plugin_client, run_id)
    assert terminal["type"] == "run.failed"  # type: ignore[union-attr]
    assert terminal["error_kind"] == "strategy"  # type: ignore[union-attr]
    assert "RuntimeError" in terminal["error"]  # type: ignore[operator]


@pytest.mark.db
async def test_de_allowlisted_create_422s_but_pinned_version_still_runs(
    mock: MockModelProvider, monkeypatch
):
    monkeypatch.setenv(ALLOWLIST, "plan_execute")
    first = AppContainer.from_settings(Settings(), mock_provider=mock)
    await _truncate(first)
    # No worker on the first container: the queue is shared (ADR 0008), and
    # any worker with the plugin allow-listed would claim the run before the
    # de-allow-listed one could — the test needs the second worker's resolve.
    app = create_app(first.settings)
    app.state.container = first
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/agents", json=CREATE_BODY)
            assert resp.status_code == 201
            agent_id = resp.json()["definition"]["id"]
    finally:
        await first.aclose()

    # de-allow-list: a NEW container sees no plugins at all
    monkeypatch.delenv(ALLOWLIST)
    second = AppContainer.from_settings(Settings(), mock_provider=mock)
    await second.start_worker()
    app2 = create_app(second.settings)
    app2.state.container = second
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://test"
        ) as client:
            resp = await client.post("/v1/agents", json=CREATE_BODY)
            assert resp.status_code == 422
            assert "plan_execute" in resp.json()["error"]["message"]

            # the pinned version still runs — snapshots outlive plugins
            # (D36): persisted terminal strategy failure, worker acked.
            resp = await client.post(f"/v1/agents/{agent_id}/run", json={"input": "go"})
            assert resp.status_code == 200
            run_id = resp.json()["run_id"]
            assert resp.json()["status"] == "failed"
            detail = (await client.get(f"/v1/executions/{run_id}")).json()
            assert detail["run"]["status"] == "failed"
            terminal = await _terminal_event(client, run_id)
            assert terminal["type"] == "run.failed"  # type: ignore[union-attr]
            assert terminal["error_kind"] == "strategy"  # type: ignore[union-attr]
    finally:
        await second.aclose()
