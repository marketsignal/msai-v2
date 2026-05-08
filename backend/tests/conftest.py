"""Shared test fixtures for the MSAI v2 test suite."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.core.auth import get_current_user
from msai.main import app
from msai.services.symbol_onboarding.partition_index import PartitionRow

# Note: setup_logging() (called at msai.main import) disables
# cache_logger_on_first_use when ENVIRONMENT=="test", which the CI job
# sets and which tests expect. This lets structlog.testing.capture_logs()
# swap in its own processor chain at any point during the test run,
# regardless of which loggers have already been bound by earlier imports.

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


@pytest.fixture(autouse=True)
def _override_auth() -> Generator[None, None, None]:
    """Override get_current_user for all tests so auth-protected endpoints pass.

    Individual test modules can add further overrides (e.g. mock DB) on top
    of this one.  The autouse cleanup restores the overrides dict after each test.
    """
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def client() -> httpx.AsyncClient:
    """Async test client wired to the MSAI FastAPI application."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# Parquet / partition_index test helpers (Scope-B coverage refactor)
# ---------------------------------------------------------------------------


@pytest.fixture
def write_partition() -> Callable[..., Path]:
    """Helper to write a real Parquet partition file under
    ``data_root/parquet/<asset>/<symbol>/<YYYY>/<MM>.parquet`` with
    one bar per requested day at 16:00 UTC.

    Returns the path to the written file. Pair with
    :func:`make_partition_row_from_path` to seed the
    ``parquet_partition_index`` cache.
    """

    def _write(
        data_root: Path,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
        days: list[int],
    ) -> Path:
        base = data_root / "parquet" / asset_class / symbol / str(year)
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{month:02d}.parquet"
        timestamps = [datetime(year, month, d, 16, 0, tzinfo=UTC) for d in days]
        df = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [1.0] * len(days),
                "high": [1.1] * len(days),
                "low": [0.9] * len(days),
                "close": [1.0] * len(days),
                "volume": [100] * len(days),
            }
        )
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
        return path

    return _write


def make_partition_row_from_path(
    path: Path,
    *,
    asset_class: str,
    symbol: str,
    year: int,
    month: int,
    days: list[int],
) -> PartitionRow:
    """Build a ``PartitionRow`` mirroring what ``read_parquet_footer`` would
    return for ``path`` written via :func:`write_partition` with the same
    ``days`` list.

    Tests pre-seed the ``parquet_partition_index`` cache by passing this
    row to ``PartitionIndexGateway.upsert``. ARRANGE-only — never use in
    VERIFY (the test-isolation rule per ``rules/critical-rules.md``).
    """
    stat = path.stat()
    timestamps = [datetime(year, month, d, 16, 0, tzinfo=UTC) for d in days]
    return PartitionRow(
        asset_class=asset_class,
        symbol=symbol,
        year=year,
        month=month,
        min_ts=min(timestamps),
        max_ts=max(timestamps),
        row_count=len(days),
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )


# ---------------------------------------------------------------------------
# Integration test fixtures (testcontainers or CI service containers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    """Provide a real PostgreSQL URL — from env var (CI) or testcontainers (local)."""
    existing = os.getenv("DATABASE_URL")
    if existing:
        yield existing
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> Generator[str, None, None]:
    """Provide a real Redis URL — from env var (CI) or testcontainers (local)."""
    existing = os.getenv("REDIS_URL")
    if existing:
        yield existing
        return

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        yield redis.get_connection_url()
