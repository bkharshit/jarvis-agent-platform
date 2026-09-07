"""Integration fixtures — require PostgreSQL (compose: `make test-db`).

Tests use a dedicated `jarvis_test` database so a dev `jarvis` database is
never touched, and tables are truncated between tests. The session prep is
deliberately sync: alembic's env.py drives its own event loop, so it must
not run inside the pytest event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from jarvis.api.app import create_app
from jarvis.api.deps import AppContainer
from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition, ModelRef, StrategyConfig
from jarvis.models.mock import MockModelProvider

TEST_DB = "jarvis_test"
ADMIN_URL = "postgresql://jarvis:jarvis@localhost:5432/jarvis"
TEST_DB_URL = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis_test"


def _alembic_config():
    from alembic.config import Config

    import jarvis.persistence

    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(jarvis.persistence.__file__).parent / "migrations"),
    )
    return config


def _create_test_db() -> None:
    import asyncpg

    async def go() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
            if not exists:
                await conn.execute(f"CREATE DATABASE {TEST_DB}")
        finally:
            await conn.close()

    asyncio.run(go())


@pytest.fixture(scope="session", autouse=True)
def _prepared_database():
    """Point Settings at the test DB, ensure it exists, migrate to head."""
    os.environ["JARVIS_DATABASE_URL"] = TEST_DB_URL
    _create_test_db()
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    yield
    os.environ.pop("JARVIS_DATABASE_URL", None)


@pytest.fixture
def mock() -> MockModelProvider:
    """Shared scripted provider — every run in the test resolves to it."""
    return MockModelProvider()


async def _truncate(container: AppContainer) -> None:
    from jarvis.persistence.models import DEFAULT_TENANT, Base

    async with container.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        # The default tenant is environmental seed data (migration 0004) —
        # NOT NULL tenant_id FKs on executions/conversations need it back.
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, created_at) "
                "VALUES (:id, 'Default', now()) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": DEFAULT_TENANT},
        )


@pytest_asyncio.fixture
async def container(mock: MockModelProvider):
    # _env_file=None: tests are hermetic — the developer's ./.env (secrets,
    # plugin allow-list) must not leak into expectations built for defaults.
    container = AppContainer.from_settings(Settings(_env_file=None), mock_provider=mock)
    await _truncate(container)
    # ASGITransport skips the lifespan, so the embedded worker is started
    # here — the routes are queue-backed (ADR 0008) and runs need a worker.
    await container.start_worker()
    yield container
    await container.aclose()


@pytest_asyncio.fixture
async def agent(container: AppContainer) -> AgentDefinition:
    definition = AgentDefinition(
        id=str(uuid4()),
        name="test-agent",
        model=ModelRef(provider="mock", model="mock-model"),
        strategy=StrategyConfig(type="function_calling"),
    )
    await container.agents.create(definition)
    return definition


@pytest_asyncio.fixture
async def app(container: AppContainer):
    app = create_app(container.settings)
    # ASGITransport does not run lifespan — hand it the container directly.
    app.state.container = container
    return app


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def parse_sse(body: str) -> list[tuple[int, str, dict[str, object]]]:
    """Parse an SSE body into (id, event, data) frames."""
    frames = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            field, _, value = line.partition(": ")
            fields[field] = value
        frames.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return frames


def turn(*args: Any, **kw: Any):
    from jarvis.models.mock import turn as mock_turn

    return mock_turn(*args, **kw)
