from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from msai.models import Base


@pytest.mark.asyncio
async def test_model_metadata_creates_tables(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _list_tables(sync_conn):
            return set(inspect(sync_conn).get_table_names())

        tables = await conn.run_sync(_list_tables)

    await engine.dispose()

    expected = {
        "users",
        "strategies",
        "backtests",
        "instrument_definitions",
        "live_deployments",
        "live_order_events",
        "trades",
        "strategy_daily_pnl",
        "audit_log",
    }
    assert expected.issubset(tables)
