"""Tenancy bootstrap commands (S2, ADR 0009 §9): tenant/user/api-key create.

No DB — the container is a recording stub injected through the CLI's
`_container` seam. What the unit suite locks: argument surface, role
validation, the "already exists / not found" exits, the one-time plaintext
print, and the fact that a password never survives the call except as a
scrypt hash."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from jarvis.cli.main import app
from jarvis.domain.auth import ApiKeyRecord, UserAccount
from jarvis.security import generate_api_key

runner = CliRunner()


class _StubAuth:
    def __init__(self):
        self.tenants: dict[str, str] = {}
        self.users: dict[str, UserAccount] = {}
        self.keys: list[dict] = []

    async def create_tenant(self, tenant_id: str, name: str) -> None:
        self.tenants[tenant_id] = name

    async def get_tenant(self, tenant_id: str):
        if tenant_id in self.tenants:
            return SimpleRow(tenant_id, self.tenants[tenant_id])
        return None

    async def get_user_by_email(self, email: str):
        return self.users.get(email)

    async def create_user(
        self, *, tenant_id, email, display_name="", password_hash=None, role="member"
    ):
        user = UserAccount(
            id="u1",
            tenant_id=tenant_id,
            email=email,
            display_name=display_name,
            role=role,
            password_hash=password_hash,
            created_at=datetime.now(UTC),
        )
        self.users[email] = user
        return user

    async def create_api_key(self, *, tenant_id, user_id, name, key_hash, key_prefix):
        record = ApiKeyRecord(
            id="k1",
            tenant_id=tenant_id,
            user_id=user_id,
            name=name,
            key_prefix=key_prefix,
            created_at=datetime.now(UTC),
        )
        self.keys.append({"tenant_id": tenant_id, "user_id": user_id, "name": name})
        return record


class SimpleRow:
    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name


class _StubStrategies:
    def names(self) -> list[str]:
        return ["function_calling", "react"]


class _StubAgents:
    def __init__(self):
        self.created: list = []

    async def create(self, definition) -> None:
        self.created.append(definition)


class _StubContainer:
    def __init__(self) -> None:
        self.auth = _StubAuth()
        self.strategies = _StubStrategies()
        self.agents = _StubAgents()

    async def aclose(self) -> None:
        pass


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubAuth:
    from jarvis.cli.main import console

    console.width = 200  # keep the long key line unwrapped in CliRunner output
    container = _StubContainer()
    container.auth.tenants["acme"] = "Acme Corp"  # pre-existing tenant
    monkeypatch.setattr("jarvis.cli.main._container", lambda: container)
    return container.auth


def test_tenant_create(stub):
    result = runner.invoke(app, ["tenant", "create", "globex", "Globex"])
    assert result.exit_code == 0
    assert "globex" in result.output and "Globex" in result.output
    assert stub.tenants["globex"] == "Globex"


def test_tenant_create_rejects_duplicate(stub):
    result = runner.invoke(app, ["tenant", "create", "acme"])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_user_create_hashes_password_and_never_echoes_it(stub):
    result = runner.invoke(
        app,
        ["user", "create", "acme", "owner@acme.test", "--role", "owner", "--password", "hunter2"],
    )
    assert result.exit_code == 0, result.output
    assert "hunter2" not in result.output
    user = stub.users["owner@acme.test"]
    assert user.password_hash is not None and user.password_hash != "hunter2"
    assert user.role == "owner"


def test_user_create_without_password_is_keys_only(stub):
    result = runner.invoke(app, ["user", "create", "acme", "keys@acme.test"])
    assert result.exit_code == 0
    assert stub.users["keys@acme.test"].password_hash is None


def test_user_create_rejects_bad_role(stub):
    result = runner.invoke(app, ["user", "create", "acme", "x@acme.test", "--role", "superuser"])
    assert result.exit_code == 1
    assert "owner|admin|member" in result.output


def test_user_create_requires_existing_tenant(stub):
    result = runner.invoke(app, ["user", "create", "ghost", "x@ghost.test"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_api_key_plaintext_printed_once(stub):
    from jarvis.security import hash_password

    stub.users["owner@acme.test"] = UserAccount(
        id="u1",
        tenant_id="acme",
        email="owner@acme.test",
        display_name="",
        role="owner",
        password_hash=hash_password("pw"),
        created_at=datetime.now(UTC),
    )
    result = runner.invoke(app, ["api-key", "create", "owner@acme.test", "--name", "ci"])
    assert result.exit_code == 0, result.output
    # exactly one jarvis_sk_ plaintext appears, with the store-it warning
    assert result.output.count("jarvis_sk_") == 1
    key_lines = [ln for ln in result.output.splitlines() if ln.strip().startswith("key:")]
    plaintext = key_lines[0].split()[-1]
    assert plaintext.startswith("jarvis_sk_")
    assert len(stub.keys) == 1 and stub.keys[0]["user_id"] == "u1"
    assert generate_api_key() != plaintext  # sanity: random generator ran


def test_api_key_unknown_email_fails(stub):
    result = runner.invoke(app, ["api-key", "create", "nobody@acme.test"])
    assert result.exit_code == 1
    assert "no user" in result.output


# --- agent create: D36 create-boundary strategy validation ------------------


@pytest.fixture
def cli_container(monkeypatch: pytest.MonkeyPatch) -> _StubContainer:
    from jarvis.cli.main import console

    console.width = 200
    container = _StubContainer()
    monkeypatch.setattr("jarvis.cli.main._container", lambda: container)
    return container


def test_agent_create_rejects_unknown_strategy_type(tmp_path, cli_container):
    yaml_file = tmp_path / "agent.yaml"
    yaml_file.write_text(
        "name: plugin-agent\nmodel: {provider: mock, model: m}\nstrategy: {type: plan_execute}\n"
    )
    result = runner.invoke(app, ["agent", "create", "--file", str(yaml_file)])
    assert result.exit_code == 1
    assert "plan_execute" in result.output  # the typo
    assert "function_calling" in result.output  # the known list
    assert cli_container.agents.created == []


def test_agent_create_accepts_known_strategy(tmp_path, cli_container):
    yaml_file = tmp_path / "agent.yaml"
    yaml_file.write_text(
        "name: plain-agent\nmodel: {provider: mock, model: m}\nstrategy: {type: react}\n"
    )
    result = runner.invoke(app, ["agent", "create", "--file", str(yaml_file)])
    assert result.exit_code == 0, result.output
    assert len(cli_container.agents.created) == 1
    assert cli_container.agents.created[0].strategy.type == "react"
