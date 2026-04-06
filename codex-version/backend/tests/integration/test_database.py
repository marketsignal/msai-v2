from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_database_connection(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()
