"""Migrations: up/down/up and schema presence. Sync tests on purpose —
alembic's env.py runs its own event loop."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import TEST_DB_URL, _alembic_config

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "agents",
    "agent_versions",
    "agent_executions",
    "conversations",
    "messages",
    "tool_executions",
    "execution_events",
}


def test_migrations_up_down_up() -> None:
    from alembic import command

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # idempotent re-run

    async def check() -> set[str]:
        engine = create_async_engine(TEST_DB_URL)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    tables = asyncio.run(check())
    assert EXPECTED_TABLES <= tables
