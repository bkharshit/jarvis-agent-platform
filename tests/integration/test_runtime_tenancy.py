"""Tenant isolation through the run path, end to end (S2, ADR 0008/0009).

The worker rebuilds the run from the queue message alone — so the tenant
stamp and principal on that message decide stored-credential resolution.
These tests drive real runs (queued → worker → runtime → provider):

- the owning tenant's run decrypts the BYOK credential and the decrypted
  material reaches the provider as the Bearer key (and nowhere else);
- a foreign tenant's run with the same credential id ends in a persisted
  terminal `model` failure (D5) with no existence leak.

respx intercepts the provider's HTTP call — the mock provider cannot play
this role (it carries no credential seam)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid

import pytest
import respx

from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig
from jarvis.security import (
    encrypt,
    generate_api_key,
    hash_api_key,
    hash_password,
    key_prefix,
    load_master_key,
)

pytestmark = pytest.mark.db

TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}
MASTER_KEY_ENV = "JARVIS_CREDENTIALS_MASTER_KEY"
SECRET = "sk-e2e-byok-material-9876543210"

COMPLETION = {
    "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    "model": "byok-model",
}


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    monkeypatch.setenv(MASTER_KEY_ENV, key)


def _agent_definition(name: str, credential_id: str) -> AgentDefinition:
    return AgentDefinition(
        id=str(uuid.uuid4()),
        name=name,
        model=ModelRef(
            provider="openai_compatible",
            model="byok-model",
            base_url="http://byok.test/v1",
            credential_ref={"type": "stored", "credential_id": credential_id},
        ),
        strategy=StrategyConfig(type="function_calling"),
    )


async def _tenant_with_key(container, tenant_id: str) -> tuple[str, str]:
    """Tenant + owner + API key — returns (user_id, Bearer plaintext)."""
    await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")
    user = await container.auth.create_user(
        tenant_id=tenant_id,
        email=f"owner@{tenant_id}.test",
        password_hash=hash_password("pw"),
        role="owner",
    )
    plaintext = generate_api_key()
    await container.auth.create_api_key(
        tenant_id=tenant_id,
        user_id=user.id,
        name="e2e",
        key_hash=hash_api_key(plaintext),
        key_prefix=key_prefix(plaintext),
    )
    return user.id, plaintext


async def _await_terminal(client, run_id: str, headers=None, timeout_s: float = 10.0):
    """Poll the execution until a terminal state (D5: one always arrives)."""
    for _ in range(int(timeout_s / 0.1)):
        resp = await client.get(f"/v1/executions/{run_id}", headers=headers or {})
        if resp.status_code == 200 and resp.json()["run"]["status"] in TERMINAL:
            return resp
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


@pytest.mark.db
@respx.mock
async def test_stored_credential_resolves_in_worker_run(client, container):
    secret_header = {}

    def _capture(request):
        secret_header["authorization"] = request.headers.get("Authorization")
        return respx.MockResponse(status_code=200, json=COMPLETION)

    respx.post("http://byok.test/v1/chat/completions").mock(side_effect=_capture)

    user_id, bearer = await _tenant_with_key(container, "acme")
    settings = Settings()
    envelope = encrypt(SECRET, master_key=load_master_key(settings))
    cred = await container.auth.create_credential(
        tenant_id="acme",
        name="acme-byok",
        provider="openai_compatible",
        ciphertext=envelope,
        created_by=user_id,
    )
    definition = _agent_definition("acme-agent", cred.id)
    await container.agents.create(definition, tenant_id="acme")

    headers = {"Authorization": f"Bearer {bearer}"}
    run = await client.post(
        f"/v1/agents/{definition.id}/run", json={"input": "hi"}, headers=headers
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]

    terminal = await _await_terminal(client, run_id, headers)
    assert terminal.json()["run"]["status"] == "succeeded"
    assert terminal.json()["run"]["tenant_id"] == "acme"

    # the decrypted material reached the provider — and only the provider
    assert secret_header["authorization"] == f"Bearer {SECRET}"

    # the transcript/events never hold the material (write-only credential)
    detail = await client.get(f"/v1/executions/{run_id}", headers=headers)
    assert SECRET not in json.dumps(detail.json())

    # the run row is tenant-scoped: a foreign principal gets 404
    foreign_user, foreign_bearer = await _tenant_with_key(container, "acme-b")
    foreign = await client.get(
        f"/v1/executions/{run_id}", headers={"Authorization": f"Bearer {foreign_bearer}"}
    )
    assert foreign.status_code == 404, f"whoami={foreign.text}"


@pytest.mark.db
async def test_foreign_tenant_credential_id_ends_in_model_failure(client, container):
    user_id, bearer = await _tenant_with_key(container, "acme")
    settings = Settings()
    cred = await container.auth.create_credential(
        tenant_id="acme",
        name="acme-byok",
        provider="openai_compatible",
        ciphertext=encrypt(SECRET, master_key=load_master_key(settings)),
        created_by=user_id,
    )
    # tenant "other" binds an agent to acme's credential id — the id alone
    # is never authorization (ADR 0006 §10)
    _, other_bearer = await _tenant_with_key(container, "other")
    definition = _agent_definition("other-agent", cred.id)
    await container.agents.create(definition, tenant_id="other")

    headers = {"Authorization": f"Bearer {other_bearer}"}
    run = await client.post(
        f"/v1/agents/{definition.id}/run", json={"input": "hi"}, headers=headers
    )
    assert run.status_code == 200
    terminal = await _await_terminal(client, run.json()["run_id"], headers)

    body = terminal.json()
    assert body["run"]["status"] == "failed"
    assert body["run"]["error_kind"] == "model"
    # no existence leak: the foreign id resolves to "not found"
    assert "not found" in body["run"]["error"]
    assert SECRET not in json.dumps(body)
