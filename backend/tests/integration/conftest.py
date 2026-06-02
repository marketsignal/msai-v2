"""Integration-test conftest -- re-exports shared portfolio-backtest fixtures.

Existing per-module Postgres fixtures (``isolated_postgres_url`` declared
inline in test files like ``test_portfolio_service.py``,
``test_portfolio_job_orchestration.py``, ``test_portfolio_full_lifecycle.py``)
keep working unchanged because pytest resolves fixtures locally first.
This file only adds NEW fixtures (portfolio_postgres_url,
portfolio_session_factory, portfolio_db_session, api_client_authed,
make_strategy, make_portfolio_with_strategies) that the portfolio-backtest
F1/F1c/F3/F4 tests use.

The canonical fixture source lives in ``conftest_portfolio_backtest.py``
(non-conftest filename) so pytest's auto-discovery does not pick it up
at the parent level and cross-pollute other test families.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration._alembic_subprocess import run_alembic as _run_alembic
from tests.integration.conftest_portfolio_backtest import (  # noqa: F401
    _seed_user,
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
    make_portfolio_run,
    make_portfolio_with_strategies,
    make_strategy,
    portfolio_db_session,
    portfolio_postgres_url,
    portfolio_session_factory,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(scope="module")
def broker_postgres_url() -> Iterator[str]:
    """Dedicated module-scoped Postgres testcontainer for broker-account tests.

    A separate container (not the portfolio one) because broker-account
    tests apply the schema via ``alembic upgrade head`` rather than
    ``Base.metadata.create_all`` — the ``broker_accounts`` partial-unique
    indexes (``uq_broker_accounts_active_ib_account_id`` /
    ``uq_broker_accounts_active_gateway_slot``) are defined ONLY in the
    migration, so ``create_all`` would not produce them.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture(scope="module")
async def _broker_migrated_url(broker_postgres_url: str) -> str:
    """Apply ``alembic upgrade head`` once per module against the container.

    The ``broker_accounts`` partial indexes and ``live_deployments`` table
    are created here so every per-test session sees the production schema.
    """
    _run_alembic(broker_postgres_url, "upgrade", "head")
    return broker_postgres_url


@pytest_asyncio.fixture
async def broker_db_session(_broker_migrated_url: str) -> AsyncIterator[AsyncSession]:
    """Single AsyncSession with per-test table isolation.

    The schema is migrated once per module; this fixture TRUNCATEs the
    tables broker-account tests touch before each test so rows do not leak
    across tests sharing the same container (e.g. a duplicate
    ``ib_account_id`` or an already-allocated slot).
    """
    from sqlalchemy import text

    engine = create_async_engine(_broker_migrated_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE broker_accounts, live_deployments, "
                    "live_portfolio_revisions, live_portfolios RESTART IDENTITY CASCADE"
                )
            )
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()
