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
    "run_queue",
    "run_cancels",
    "tenants",
    "users",
    "sessions",
    "api_keys",
    "credentials",
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

    async def enum_values() -> list[str]:
        engine = create_async_engine(TEST_DB_URL)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT unnest(enum_range(NULL::execution_status))::text ORDER BY 1")
                )
                return [row[0] for row in rows]
        finally:
            await engine.dispose()

    values = asyncio.run(enum_values())
    # S1 (migration 0002): runs are enqueued before a worker claims them.
    assert "queued" in values


def test_credential_ref_snapshot_rewrite() -> None:
    """S2 (0005): api_key_env → credential_ref {type: env} — env-var names
    only, plaintext never enters a snapshot; down restores env refs.
    Alembic commands run in sync context (conftest note), async seeds/checks
    interleave via asyncio.run."""
    import json

    from alembic import command

    config = _alembic_config()

    async def seed() -> None:
        engine = create_async_engine(TEST_DB_URL)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO agents (id, name, description, current_version,"
                        " created_at, updated_at) VALUES"
                        " ('a-s2', 's2-migration', '', 1, now(), now())"
                    )
                )
                # Old-shape snapshot (pre-S2): api_key_env on the model block.
                await conn.execute(
                    text(
                        "INSERT INTO agent_versions (id, agent_id, version, snapshot,"
                        " label, created_at) VALUES ('v-s2', 'a-s2', 1, :snapshot,"
                        " '', now())"
                    ),
                    {
                        "snapshot": json.dumps(
                            {
                                "id": "a-s2",
                                "name": "s2-migration",
                                "model": {
                                    "provider": "openai_compatible",
                                    "model": "gpt-4o-mini",
                                    "api_key_env": "OPENAI_API_KEY",
                                },
                                "strategy": {"type": "function_calling"},
                            }
                        )
                    },
                )
                # Old-shape snapshot with an EXPLICIT null api_key_env (the
                # pre-S2 serializer emitted it for credential-less models).
                await conn.execute(
                    text(
                        "INSERT INTO agent_versions (id, agent_id, version, snapshot,"
                        " label, created_at) VALUES ('v-s2-null', 'a-s2', 2, :snapshot,"
                        " '', now())"
                    ),
                    {
                        "snapshot": json.dumps(
                            {
                                "id": "a-s2",
                                "name": "s2-migration",
                                "model": {
                                    "provider": "mock",
                                    "model": "mock-agent",
                                    "api_key_env": None,
                                },
                                "strategy": {"type": "function_calling"},
                            }
                        )
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    command.downgrade(config, "0004")
    command.upgrade(config, "0005")

    async def check() -> None:
        engine = create_async_engine(TEST_DB_URL)
        try:
            async with engine.connect() as conn:
                row = await conn.execute(
                    text("SELECT snapshot FROM agent_versions WHERE id = 'v-s2'")
                )
                snapshot = row.scalar_one()
                row = await conn.execute(
                    text("SELECT snapshot FROM agent_versions WHERE id = 'v-s2-null'")
                )
                null_snapshot = row.scalar_one()
        finally:
            await engine.dispose()

        model = snapshot["model"]
        assert "api_key_env" not in model
        assert model["credential_ref"] == {"type": "env", "env_var": "OPENAI_API_KEY"}

        # Regression (found live in the S2 walkthrough): pre-S2 snapshots
        # serialized `api_key_env: null` for credential-less models. The
        # migration must match on VALUE, not key presence — a null must be
        # dropped, not rewritten into credential_ref {type: env, env_var:
        # null} (unparseable by the discriminated union; 500s every listing).
        null_model = null_snapshot["model"]
        assert "api_key_env" not in null_model
        assert "credential_ref" not in null_model

    asyncio.run(check())
