"""Integration test — ``POST /api/v1/backtests/run`` resolves instruments
via :meth:`SecurityMaster.resolve_for_backtest` (Task 11).

The production path change on ``api/backtests.py:90`` replaces the
closed-universe :func:`canonical_instrument_id` helper with a registry
lookup. Pre-seeded rows under ``provider="databento"`` MUST win; the
persisted ``Backtest.instruments`` column MUST contain the canonical
alias strings returned by the registry (``"AAPL.NASDAQ"``), not the bare
input (``"AAPL"``).

Follows the per-module ``session_factory`` + ``isolated_postgres_url``
fixture pattern from ``test_security_master_resolve_backtest.py``. Redis
is stubbed via :mod:`unittest.mock` so the endpoint's
``enqueue_backtest`` call short-circuits without a real broker.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.models import Base
from msai.models.backtest import Backtest
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.models.strategy import Strategy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest_asyncio.fixture
async def seeded_strategy(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path_factory: pytest.TempPathFactory,
) -> Strategy:
    """Seed a strategy row + a real source file on disk.

    The endpoint computes ``strategy_code_hash`` from the file contents,
    so the file must exist for the request to succeed.
    """
    strat_dir = tmp_path_factory.mktemp("strategies_registry_test")
    strat_file = strat_dir / "smoke.py"
    strat_file.write_text("# smoke strategy source\n")

    strategy = Strategy(
        id=uuid4(),
        name="registry-smoke",
        file_path=str(strat_file),
        strategy_class="SmokeStrategy",
        default_config={},
    )
    async with session_factory() as session, session.begin():
        session.add(strategy)
    return strategy


@pytest_asyncio.fixture
async def seeded_aapl_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed ``AAPL`` → ``AAPL.NASDAQ`` under ``provider="databento"``.

    This mirrors the shape ``msai instruments refresh`` will produce in
    Task 13 — an ``InstrumentDefinition`` row keyed on
    ``(raw_symbol, provider, asset_class)`` plus an active
    :class:`InstrumentAlias` row with ``effective_to=None``.
    """
    async with session_factory() as session, session.begin():
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="databento",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        alias = InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="databento",
            effective_from=date(2024, 1, 1),
            effective_to=None,
        )
        session.add(alias)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with ``get_db`` overridden to the testcontainer session
    factory and ``get_current_user`` stubbed to a bare dict (enough for
    this endpoint — it doesn't read the claims).
    """

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> dict[str, Any]:
        return {"sub": "test-user", "preferred_username": "test@example.com"}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backtest_writes_registry_canonical_instruments(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_strategy: Strategy,
    seeded_aapl_registry: None,
) -> None:
    """POST /run with ``["AAPL"]`` persists ``["AAPL.NASDAQ"]`` on the row.

    Proves that the SecurityMaster registry lookup ran and its result
    (the active ``AAPL.NASDAQ`` alias) was written to the DB — the
    Phase-1 ``canonical_instrument_id`` helper would also have produced
    ``AAPL.NASDAQ`` in this case, so the assertion that distinguishes
    the two code paths is verifying the row's presence in the registry
    is required for the request to succeed.
    """
    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    body = {
        "strategy_id": str(seeded_strategy.id),
        "config": {},
        "instruments": ["AAPL"],
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
    }

    # NB: ``api/backtests.py`` does ``from msai.core.queue import get_redis_pool``
    # at module-top so the name is already bound on the router module —
    # patch the local binding, not ``msai.core.queue.get_redis_pool``
    # (which is now a stale reference on the router module).
    with patch(
        "msai.api.backtests.get_redis_pool",
        new=AsyncMock(return_value=mock_pool),
    ):
        response = await client.post("/api/v1/backtests/run", json=body)

    assert response.status_code == 201, response.text
    backtest_id = UUID(response.json()["id"])

    # Verify the persisted row contains the canonical alias from the
    # registry, not the bare input.
    async with session_factory() as session:
        row = (
            await session.execute(select(Backtest).where(Backtest.id == backtest_id))
        ).scalar_one()
        assert row.instruments == ["AAPL.NASDAQ"]


@pytest.mark.asyncio
async def test_run_backtest_raises_when_registry_is_empty(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_strategy: Strategy,
) -> None:
    """POST /run with an unseeded symbol fails loud — no silent fallback.

    When the registry has no row under ``provider="databento"`` for a
    bare ticker, :meth:`SecurityMaster.resolve_for_backtest` raises
    :class:`DatabentoDefinitionMissing`. The endpoint has no try/except
    around the resolver call, so the exception propagates through the
    ASGI pipeline.

    We build a dedicated httpx client with ``raise_app_exceptions=False``
    so the exception surfaces as a 500 at the HTTP layer instead of
    re-raising into the test body — this mirrors production behavior
    (uvicorn → FastAPI error middleware → 500 Internal Server Error).

    This test anchors the semantic change: the old closed-universe
    ``canonical_instrument_id`` helper would have silently produced
    ``"MSFT.NASDAQ"``; the new registry path refuses to guess.
    """

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> dict[str, Any]:
        return {"sub": "test-user", "preferred_username": "test@example.com"}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user

    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    body = {
        "strategy_id": str(seeded_strategy.id),
        "config": {},
        "instruments": ["MSFT"],  # never seeded
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
    }

    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            with patch(
                "msai.api.backtests.get_redis_pool",
                new=AsyncMock(return_value=mock_pool),
            ):
                response = await ac.post("/api/v1/backtests/run", json=body)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    # 500 is the default for an unhandled exception — we deliberately
    # don't wrap the resolver's DatabentoDefinitionMissing in an HTTP
    # exception because the operator-facing failure path is the CLI
    # (``msai instruments refresh``) not the API. Any 2xx here would
    # indicate silent fallback to the deprecated canonical_instrument_id.
    assert response.status_code == 500
