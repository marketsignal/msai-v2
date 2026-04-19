"""Integration tests for :class:`InstrumentCache` (Phase 2 task 2.2).

Dedicated PostgresContainer per module so the destructive
drop_all/create_all fixture can't touch a configured DATABASE_URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest.mark.asyncio
async def test_insert_and_roundtrip_equity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: insert an equity cache row with full JSONB
    fields, query it back, verify every column."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        row = InstrumentCache(
            canonical_id="AAPL.NASDAQ",
            asset_class="equity",
            venue="NASDAQ",
            ib_contract_json={
                "secType": "STK",
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "currency": "USD",
                "conId": 265598,
            },
            nautilus_instrument_json={
                "type": "Equity",
                "instrument_id": "AAPL.NASDAQ",
                "raw_symbol": "AAPL",
            },
            trading_hours={
                "timezone": "America/New_York",
                "rth": [
                    {"day": "MON", "open": "09:30", "close": "16:00"},
                    {"day": "TUE", "open": "09:30", "close": "16:00"},
                ],
                "eth": [],
            },
            last_refreshed_at=now,
        )
        session.add(row)

    async with session_factory() as session:
        fetched = (
            await session.execute(
                select(InstrumentCache).where(InstrumentCache.canonical_id == "AAPL.NASDAQ")
            )
        ).scalar_one()
        assert fetched.asset_class == "equity"
        assert fetched.venue == "NASDAQ"
        assert fetched.ib_contract_json["secType"] == "STK"
        assert fetched.ib_contract_json["conId"] == 265598
        assert fetched.nautilus_instrument_json["type"] == "Equity"
        assert fetched.trading_hours is not None
        assert fetched.trading_hours["timezone"] == "America/New_York"
        assert len(fetched.trading_hours["rth"]) == 2
        assert fetched.trading_hours["rth"][0]["open"] == "09:30"


@pytest.mark.asyncio
async def test_primary_key_uniqueness_rejects_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``canonical_id`` is the primary key — two rows with the same
    id violate the constraint. The SecurityMaster upsert path relies
    on this to catch accidental duplicates."""
    now = datetime.now(UTC)
    row_kwargs = {
        "canonical_id": "AAPL.NASDAQ",
        "asset_class": "equity",
        "venue": "NASDAQ",
        "ib_contract_json": {"secType": "STK"},
        "nautilus_instrument_json": {"type": "Equity"},
        "trading_hours": None,
        "last_refreshed_at": now,
    }
    async with session_factory() as session, session.begin():
        session.add(InstrumentCache(**row_kwargs))

    # ``pytest.raises`` isn't an async context manager, so the
    # duplicate-insert flush has to be awaited inside an explicit
    # try/except rather than in a ``with pytest.raises(...)`` block.
    duplicate_raised = False
    try:
        async with session_factory() as session, session.begin():
            session.add(InstrumentCache(**row_kwargs))
    except IntegrityError:
        duplicate_raised = True
    assert duplicate_raised


@pytest.mark.asyncio
async def test_trading_hours_nullable_for_24h_instruments(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forex pairs on IDEALPRO have no RTH/ETH distinction — the
    ``trading_hours`` column is nullable for those cases."""
    async with session_factory() as session, session.begin():
        session.add(
            InstrumentCache(
                canonical_id="EUR/USD.IDEALPRO",
                asset_class="forex",
                venue="IDEALPRO",
                ib_contract_json={"secType": "CASH"},
                nautilus_instrument_json={"type": "CurrencyPair"},
                trading_hours=None,
                last_refreshed_at=datetime.now(UTC),
            )
        )

    async with session_factory() as session:
        fetched = (
            await session.execute(
                select(InstrumentCache).where(InstrumentCache.canonical_id == "EUR/USD.IDEALPRO")
            )
        ).scalar_one()
        assert fetched.trading_hours is None


@pytest.mark.asyncio
async def test_query_by_asset_class_and_venue_uses_composite_index(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ``ix_instrument_cache_class_venue`` composite index backs
    Phase 2's bulk-resolve filter ("give me all CME futures"). We
    can't directly assert PostgreSQL uses the index, but we can
    verify the query returns the expected row set and the index
    exists in the schema."""
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                InstrumentCache(
                    canonical_id="ESM5.CME",
                    asset_class="future",
                    venue="CME",
                    ib_contract_json={"secType": "FUT"},
                    nautilus_instrument_json={"type": "FuturesContract"},
                    trading_hours=None,
                    last_refreshed_at=now,
                ),
                InstrumentCache(
                    canonical_id="NQM5.CME",
                    asset_class="future",
                    venue="CME",
                    ib_contract_json={"secType": "FUT"},
                    nautilus_instrument_json={"type": "FuturesContract"},
                    trading_hours=None,
                    last_refreshed_at=now,
                ),
                InstrumentCache(
                    canonical_id="MSFT.NASDAQ",
                    asset_class="equity",
                    venue="NASDAQ",
                    ib_contract_json={"secType": "STK"},
                    nautilus_instrument_json={"type": "Equity"},
                    trading_hours=None,
                    last_refreshed_at=now,
                ),
            ]
        )

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(InstrumentCache)
                    .where(
                        InstrumentCache.asset_class == "future",
                        InstrumentCache.venue == "CME",
                    )
                    .order_by(InstrumentCache.canonical_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert {r.canonical_id for r in rows} == {"ESM5.CME", "NQM5.CME"}


@pytest.mark.asyncio
async def test_composite_index_exists_in_schema(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: the composite ``(asset_class, venue)``
    index must exist by name — the bulk-resolve query plan
    depends on it."""
    from sqlalchemy import text

    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'instrument_cache' "
                "  AND indexname = 'ix_instrument_cache_class_venue'"
            )
        )
        assert result.scalar_one_or_none() == "ix_instrument_cache_class_venue"
