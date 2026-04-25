"""Reusable fixtures for Symbol Onboarding integration tests.

Provides:
- ``session_factory`` — testcontainers Postgres with full schema.
- ``mock_databento`` — DatabentoClient-shaped mock for bootstrap/ingest/cost.
- ``mock_ib_refresh`` — AsyncMock standing in for the IB ``msai instruments refresh`` path.
- ``tmp_parquet_root`` — tmp_path fixture seeded with fake Parquet month files.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.models import Base

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def mock_databento():
    """DatabentoClient-shaped mock. Tests customize side_effects per scenario."""
    client = MagicMock()
    client.api_key = "test-key"
    client.fetch_definition_instruments = AsyncMock()
    client.get_cost_estimate = AsyncMock(return_value=1.25)  # default cheap
    return client


@pytest.fixture
def mock_ib_refresh():
    """AsyncMock for IB instruments refresh. Defaults to success."""
    return AsyncMock(return_value=None)


@pytest.fixture
def tmp_parquet_root(tmp_path: Path) -> Path:
    """Parquet root with helper to seed fake month files.

    Tests call ``seed(asset_class, symbol, year, month)`` to create
    an empty ``.parquet`` file at the canonical path.
    """
    root = tmp_path / "parquet"
    root.mkdir()

    def seed(asset_class: str, symbol: str, year: int, month: int) -> Path:
        dir_ = root / asset_class / symbol / str(year)
        dir_.mkdir(parents=True, exist_ok=True)
        path = dir_ / f"{month:02d}.parquet"
        path.write_bytes(b"")  # empty stub; real coverage reads use pyarrow
        return path

    root.seed = seed  # type: ignore[attr-defined]
    return root
