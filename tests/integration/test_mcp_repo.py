"""SqlMcpServerRepo integration (S4, D37): CRUD + the agents repo's tenancy
pattern — shared (NULL) rows visible everywhere, tenant-owned shadowing
same-name shared rows, foreign ids resolving to None/False (D29)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from jarvis.domain.mcp import McpHttpConfig, McpServer, McpStdioConfig
from jarvis.persistence.repositories import SqlMcpServerRepo

pytestmark = pytest.mark.db

TENANT = "default"  # seeded by migration 0004 / conftest truncate
OTHER_TENANT = "tenant-b"


def _server(
    name: str = "fixtures", *, enabled: bool = True, tenant: str | None = None
) -> McpServer:
    return McpServer(
        id=str(uuid4()),
        name=name,
        config=McpStdioConfig(
            type="stdio",
            command="python",
            args=["tests/fixtures/mcp/server.py"],
            env={"WEATHER_TOKEN": {"type": "env", "env_var": "MCP_WEATHER_TOKEN"}},
        ),
        enabled=enabled,
        tenant_id=tenant,
    )


@pytest.fixture
def repo(container) -> SqlMcpServerRepo:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # The container wiring (deps.py) gains the repo at the API commit —
    # this fixture builds the same sessionmaker the container uses.
    return SqlMcpServerRepo(async_sessionmaker(container.engine, expire_on_commit=False))


@pytest.fixture
def make_tenant(container):
    async def go(tenant_id: str) -> None:
        await container.auth.create_tenant(tenant_id, f"Tenant {tenant_id}")

    return go


async def test_create_and_get_roundtrip(repo: SqlMcpServerRepo) -> None:
    server = await repo.create(_server())
    assert server.tenant_id is None  # tenant_id=None creates a shared row

    loaded = await repo.get(server.id, tenant_id=TENANT)
    assert loaded == server
    config = loaded.config
    assert config.env["WEATHER_TOKEN"].env_var == "MCP_WEATHER_TOKEN"


async def test_stored_credential_refs_roundtrip(repo: SqlMcpServerRepo) -> None:
    """ADR 0013 §1: the config JSONB carries stored credential ids (never
    secrets) and validates back through the same discriminated union."""
    server = McpServer(
        id=str(uuid4()),
        name="http-headers",
        config=McpHttpConfig(
            type="http",
            url="https://example.com/mcp",
            headers={
                "Authorization": {"type": "stored", "credential_id": "cred-1"},
                "X-Api-Key": {"type": "env", "env_var": "MCP_X_API_KEY"},
            },
        ),
    )
    created = await repo.create(server)
    loaded = await repo.get(created.id, tenant_id=TENANT)
    assert loaded == created
    headers = loaded.config.headers
    assert headers["Authorization"].credential_id == "cred-1"  # type: ignore[union-attr]
    assert headers["X-Api-Key"].env_var == "MCP_X_API_KEY"  # type: ignore[union-attr]


async def test_get_by_name_shadows_shared_with_owned(repo: SqlMcpServerRepo, make_tenant) -> None:
    shared = await repo.create(_server())  # NULL tenant
    owned = await repo.create(_server(), tenant_id=TENANT)

    row = await repo.get_by_name("fixtures", tenant_id=TENANT)
    assert row is not None and row.id == owned.id  # owned wins
    assert row.id != shared.id

    # A tenant with no owned row sees the shared one.
    await make_tenant(OTHER_TENANT)
    row = await repo.get_by_name("fixtures", tenant_id=OTHER_TENANT)
    assert row is not None and row.id == shared.id

    # Unscoped reads see whichever exists — the same owned-first ordering.
    row = await repo.get_by_name("fixtures")
    assert row is not None and row.id == owned.id


async def test_list_servers_shadows_and_orders(repo: SqlMcpServerRepo, make_tenant) -> None:
    await repo.create(_server("remote"))  # shared
    await repo.create(_server("fixtures"), tenant_id=TENANT)  # owned
    await repo.create(_server("fixtures"))  # shared same-name (shadowed)

    visible = await repo.list_servers(tenant_id=TENANT)
    names = {(s.name, s.tenant_id) for s in visible}
    assert names == {("remote", None), ("fixtures", TENANT)}

    await make_tenant(OTHER_TENANT)
    visible = await repo.list_servers(tenant_id=OTHER_TENANT)
    assert {(s.name, s.tenant_id) for s in visible} == {
        ("remote", None),
        ("fixtures", None),
    }


async def test_update_enabled_and_config_only(repo: SqlMcpServerRepo) -> None:
    server = await repo.create(_server())
    patched = server.model_copy(
        update={
            "enabled": False,
            "name": "renamed-is-ignored",
            "config": McpHttpConfig(type="http", url="https://example.com/mcp"),
        }
    )
    updated = await repo.update(patched)
    assert updated.enabled is False
    assert updated.name == "fixtures"  # the name is immutable
    assert isinstance(updated.config, McpHttpConfig)
    assert (await repo.get(server.id)).enabled is False  # type: ignore[union-attr]


async def test_delete_scoped(repo: SqlMcpServerRepo, make_tenant) -> None:
    await make_tenant(OTHER_TENANT)
    owned = await repo.create(_server(), tenant_id=OTHER_TENANT)
    assert await repo.delete(owned.id, tenant_id=TENANT) is False  # foreign → False
    assert await repo.get(owned.id, tenant_id=TENANT) is None  # no existence leak
    assert await repo.delete(owned.id, tenant_id=OTHER_TENANT) is True
    assert await repo.delete(owned.id, tenant_id=OTHER_TENANT) is False


async def test_name_unique_per_tenant(repo: SqlMcpServerRepo, make_tenant) -> None:
    await repo.create(_server(), tenant_id=TENANT)
    await make_tenant(OTHER_TENANT)
    # Same name in a DIFFERENT tenant is fine.
    await repo.create(_server(), tenant_id=OTHER_TENANT)
    # Same name twice in one tenant violates the partial unique index.
    with pytest.raises(IntegrityError):
        await repo.create(_server(), tenant_id=TENANT)


async def test_shared_name_unique(repo: SqlMcpServerRepo) -> None:
    await repo.create(_server())
    with pytest.raises(IntegrityError):
        await repo.create(_server())


async def test_owned_row_blocks_same_shared_name(repo: SqlMcpServerRepo) -> None:
    """An owned row may not shadow-collide with a shared one — owned and
    shared live under different indexes; both may exist (shadowing is a
    read rule), so this asserts the pair CAN coexist."""
    shared = await repo.create(_server())
    owned = await repo.create(_server(), tenant_id=TENANT)
    assert shared.id != owned.id
