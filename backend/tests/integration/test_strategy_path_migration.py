"""Database proof for canonical strategy registry file paths."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration._alembic_subprocess import run_alembic

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    """Use a disposable database so migration assertions cannot touch dev data."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.mark.asyncio
async def test_upgrade_normalizes_absolute_relative_and_smoke_paths(
    isolated_postgres_url: str,
) -> None:
    run_alembic(isolated_postgres_url, "upgrade", "b50fd33a0a8c")
    fixtures = {
        "absolute-container": "/app/strategies/example/ema_cross.py",
        "absolute-host": "/Users/dev/msai-v2/strategies/custom/momentum.py",
        "already-relative": "example/ema_cross.py",
        "__smoke__/ema_cross/AAPL": "strategies/example/ema_cross.py",
    }
    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.begin() as conn:
            for name, file_path in fixtures.items():
                await conn.execute(
                    sa.text(
                        """
                        INSERT INTO strategies (
                            id, name, file_path, strategy_class, created_at, updated_at
                        ) VALUES (
                            :id, :name, :file_path, 'FixtureStrategy', NOW(), NOW()
                        )
                        """
                    ),
                    {"id": uuid4(), "name": name, "file_path": file_path},
                )
    finally:
        await engine.dispose()

    run_alembic(isolated_postgres_url, "upgrade", "head")

    engine = create_async_engine(isolated_postgres_url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text("SELECT name, file_path FROM strategies WHERE name = ANY(:names)"),
                    {"names": list(fixtures)},
                )
            ).mappings()
            normalized = {str(row["name"]): str(row["file_path"]) for row in rows}
    finally:
        await engine.dispose()

    assert normalized == {
        "absolute-container": "example/ema_cross.py",
        "absolute-host": "custom/momentum.py",
        "already-relative": "example/ema_cross.py",
        "__smoke__/ema_cross/AAPL": "example/ema_cross.py",
    }
