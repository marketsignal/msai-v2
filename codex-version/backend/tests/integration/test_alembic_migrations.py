from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from msai.core.config import settings

_PORTFOLIO_SCHEMA_REVISION = "20260416_0008"
_BACKFILL_REVISION = "20260416_0009"


def test_alembic_head_is_linear_and_upgrades(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_url", postgres_url)
    _reset_database(postgres_url)

    config = _alembic_config()
    heads = ScriptDirectory.from_config(config).get_heads()

    assert heads == [_BACKFILL_REVISION]

    command.upgrade(config, "head")

    engine = create_async_engine(postgres_url)

    async def _inspect_tables() -> set[str]:
        async with engine.begin() as conn:
            return await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    tables = asyncio.run(_inspect_tables())

    assert "live_portfolios" in tables
    assert "live_deployment_strategies" in tables

    asyncio.run(engine.dispose())


def test_backfill_migration_wraps_legacy_live_deployment(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_url", postgres_url)
    _reset_database(postgres_url)

    config = _alembic_config()
    command.upgrade(config, _PORTFOLIO_SCHEMA_REVISION)

    async def _seed_legacy_deployment() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO strategies (
                        id, name, file_path, strategy_class, created_at, updated_at
                    ) VALUES (
                        :id, :name, :file_path, :strategy_class, NOW(), NOW()
                    )
                    """
                ),
                {
                    "id": "strategy-legacy",
                    "name": "example.ema_cross",
                    "file_path": "example/ema_cross.py",
                    "strategy_class": "EMACrossStrategy",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO live_deployments (
                        id,
                        strategy_id,
                        strategy_code_hash,
                        strategy_git_sha,
                        config,
                        instruments,
                        identity_signature,
                        deployment_slug,
                        strategy_id_full,
                        account_id,
                        status,
                        paper_trading,
                        ib_data_client_id,
                        ib_exec_client_id,
                        process_pid,
                        started_at,
                        stopped_at,
                        started_by,
                        created_at
                    ) VALUES (
                        :id,
                        :strategy_id,
                        :strategy_code_hash,
                        NULL,
                        CAST(:config AS JSONB),
                        CAST(:instruments AS VARCHAR[]),
                        :identity_signature,
                        :deployment_slug,
                        :strategy_id_full,
                        :account_id,
                        'running',
                        true,
                        11,
                        12,
                        4321,
                        NOW(),
                        NULL,
                        NULL,
                        NOW()
                    )
                    """
                ),
                {
                    "id": "dep-legacy-1",
                    "strategy_id": "strategy-legacy",
                    "strategy_code_hash": "hash-legacy",
                    "config": '{"trade_size":"1","instrument_id":"AAPL.XNAS"}',
                    "instruments": ["AAPL.XNAS"],
                    "identity_signature": "identity-legacy",
                    "deployment_slug": "legacy-abc123",
                    "strategy_id_full": "EMACrossStrategy-0-legacy-abc123",
                    "account_id": None,
                },
            )
        await engine.dispose()

    asyncio.run(_seed_legacy_deployment())

    command.upgrade(config, "head")

    async def _assert_backfill() -> None:
        engine = create_async_engine(postgres_url)
        async with engine.connect() as conn:
            deployment = (
                await conn.execute(
                    text(
                        """
                        SELECT portfolio_revision_id, strategy_id_full
                        FROM live_deployments
                        WHERE id = :deployment_id
                        """
                    ),
                    {"deployment_id": "dep-legacy-1"},
                )
            ).mappings().one()
            assert deployment["portfolio_revision_id"]

            portfolio = (
                await conn.execute(
                    text(
                        """
                        SELECT lp.id, lp.name, lpr.id AS revision_id
                        FROM live_portfolios lp
                        JOIN live_portfolio_revisions lpr ON lpr.portfolio_id = lp.id
                        WHERE lpr.id = :revision_id
                        """
                    ),
                    {"revision_id": deployment["portfolio_revision_id"]},
                )
            ).mappings().one()
            assert portfolio["name"] == "Legacy-legacy-abc123"

            member = (
                await conn.execute(
                    text(
                        """
                        SELECT strategy_id, order_index, instruments
                        FROM live_portfolio_revision_strategies
                        WHERE revision_id = :revision_id
                        """
                    ),
                    {"revision_id": portfolio["revision_id"]},
                )
            ).mappings().one()
            assert member["strategy_id"] == "strategy-legacy"
            assert member["order_index"] == 0
            assert member["instruments"] == ["AAPL.XNAS"]

            bridge_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT strategy_id_full
                        FROM live_deployment_strategies
                        WHERE deployment_id = :deployment_id
                        """
                    ),
                    {"deployment_id": "dep-legacy-1"},
                )
            ).mappings().all()
            assert [row["strategy_id_full"] for row in bridge_rows] == [deployment["strategy_id_full"]]
        await engine.dispose()

    asyncio.run(_assert_backfill())


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def _reset_database(postgres_url: str) -> None:
    asyncio.run(_reset_database_async(postgres_url))


async def _reset_database_async(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
