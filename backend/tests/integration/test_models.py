from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from msai.models import Base


@pytest.mark.asyncio
async def test_model_metadata_creates_tables(postgres_url: str) -> None:
    """Verify Base.metadata.create_all produces all expected tables."""
    engine = create_async_engine(postgres_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _list_tables(sync_conn: object) -> set[str]:
            return set(inspect(sync_conn).get_table_names())

        tables = await conn.run_sync(_list_tables)

    await engine.dispose()

    expected = {
        "users",
        "strategies",
        "backtests",
        "live_deployments",
        "trades",
        "strategy_daily_pnl",
        "audit_logs",
    }
    assert expected.issubset(tables)
