"""GET /v1/capabilities over HTTP: shape contract + OpenAPI listing."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db

EXPECTED_SECTIONS = {
    "agents",
    "executions",
    "conversations",
    "tools",
    "models",
    "workflows",
    "knowledge",
    "evaluations",
    "observability",
    "plugins",
    "triggers",
    "settings",
}


@pytest.mark.db
async def test_capabilities_shape(client):
    resp = await client.get("/v1/capabilities")
    assert resp.status_code == 200
    sections = resp.json()["sections"]
    assert set(sections) == EXPECTED_SECTIONS
    for key, section in sections.items():
        assert "enabled" in section
        if not section["enabled"]:
            assert section["stage"], f"disabled section {key!r} must name its stage"
    # derived details come from the real registries
    assert sections["agents"]["detail"]["strategies"] == ["function_calling", "react"]
    builtins = {t["name"] for t in sections["tools"]["detail"]["builtins"]}
    assert builtins == {
        "calculator",
        "current_time",
        "http_get",
        "memory_get",
        "memory_put",
        "memory_delete",
    }
    providers = {p["name"] for p in sections["models"]["detail"]["providers"]}
    assert providers == {"mock", "openai_compatible"}
    # S10: human-in-the-loop is live (pause frames, resume route, inbox)
    assert sections["executions"]["detail"]["human_in_the_loop"] is True


@pytest.mark.db
async def test_capabilities_in_openapi(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/v1/capabilities" in resp.json()["paths"]
