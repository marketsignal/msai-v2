"""Unit test for MarketHoursService reading instrument_definitions.trading_hours.

Replaces the legacy ``instrument_cache.trading_hours`` read path. Verifies:

1. prime() loads via the registry (instrument_definitions + instrument_aliases).
2. is_in_rth/eth fail-open on NULL (preserves legacy behavior).
3. is_in_rth correctly evaluates a window in the column's stored timezone.

Uses the project's testcontainers Postgres pattern.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentAlias, InstrumentDefinition
from msai.services.nautilus.market_hours import MarketHoursService

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
    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.mark.asyncio
async def test_prime_loads_trading_hours_from_instrument_definitions(
    session: AsyncSession,
) -> None:
    # Arrange — seed AAPL.NASDAQ with NYSE-style trading hours
    aapl_uid = uuid4()
    session.add(
        InstrumentDefinition(
            instrument_uid=aapl_uid,
            raw_symbol="AAPL",
            provider="interactive_brokers",
            asset_class="equity",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            lifecycle_state="active",
            trading_hours={
                "timezone": "America/New_York",
                "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}],
                "eth": [{"day": "MON", "open": "04:00", "close": "20:00"}],
            },
        )
    )
    session.add(
        InstrumentAlias(
            id=uuid4(),
            instrument_uid=aapl_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()

    # Act — prime the service
    svc = MarketHoursService()
    await svc.prime(session, ["AAPL.NASDAQ"])

    # Assert — Monday 10:00 ET is RTH; Monday 03:00 ET is not even ETH;
    # Monday 05:00 ET is ETH not RTH
    monday_10am_et = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)  # 10:00 ET
    monday_3am_et = datetime(2026, 4, 27, 7, 0, tzinfo=UTC)  # 03:00 ET
    monday_5am_et = datetime(2026, 4, 27, 9, 0, tzinfo=UTC)  # 05:00 ET
    assert svc.is_in_rth("AAPL.NASDAQ", monday_10am_et) is True
    assert svc.is_in_rth("AAPL.NASDAQ", monday_5am_et) is False
    assert svc.is_in_eth("AAPL.NASDAQ", monday_5am_et) is True
    assert svc.is_in_eth("AAPL.NASDAQ", monday_3am_et) is False


@pytest.mark.asyncio
async def test_prime_fail_open_on_missing_alias(session: AsyncSession) -> None:
    svc = MarketHoursService()
    await svc.prime(session, ["UNKNOWN.NASDAQ"])

    # Fail-open: never primed → True regardless of timestamp
    assert svc.is_in_rth("UNKNOWN.NASDAQ", datetime(2026, 4, 27, 14, 0, tzinfo=UTC)) is True


@pytest.mark.asyncio
async def test_prime_fail_open_on_null_trading_hours(session: AsyncSession) -> None:
    # Alias exists but trading_hours is NULL (24h venue case)
    eur_uid = uuid4()
    session.add(
        InstrumentDefinition(
            instrument_uid=eur_uid,
            raw_symbol="EUR/USD",
            provider="interactive_brokers",
            asset_class="fx",
            listing_venue="IDEALPRO",
            routing_venue="IDEALPRO",
            lifecycle_state="active",
            trading_hours=None,
        )
    )
    session.add(
        InstrumentAlias(
            id=uuid4(),
            instrument_uid=eur_uid,
            alias_string="EUR/USD.IDEALPRO",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()

    svc = MarketHoursService()
    await svc.prime(session, ["EUR/USD.IDEALPRO"])

    sunday_3am_utc = datetime(2026, 4, 26, 3, 0, tzinfo=UTC)
    assert svc.is_in_rth("EUR/USD.IDEALPRO", sunday_3am_utc) is True


@pytest.mark.asyncio
async def test_prime_filters_by_ib_provider_when_databento_alias_shares_canonical_id(
    session: AsyncSession,
) -> None:
    """Both Databento and IB aliases for the same canonical_id must
    deterministically resolve to the IB row's trading_hours. Without
    the provider filter, result-order non-determinism could cache the
    Databento row's NULL trading_hours and silently fail-open every
    market-hours check.
    """
    # Arrange — IB definition with populated NYSE-style trading hours
    aapl_ib_uid = uuid4()
    aapl_databento_uid = uuid4()
    session.add(
        InstrumentDefinition(
            instrument_uid=aapl_ib_uid,
            raw_symbol="AAPL",
            provider="interactive_brokers",
            asset_class="equity",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            lifecycle_state="active",
            trading_hours={
                "timezone": "America/New_York",
                "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}],
                "eth": [{"day": "MON", "open": "04:00", "close": "20:00"}],
            },
        )
    )
    session.add(
        InstrumentDefinition(
            instrument_uid=aapl_databento_uid,
            raw_symbol="AAPL",
            provider="databento",
            asset_class="equity",
            listing_venue="XNAS",
            routing_venue="SMART",
            lifecycle_state="active",
            trading_hours=None,  # Databento rows don't carry IB-style hours
        )
    )
    # Same alias_string (AAPL.NASDAQ) under TWO providers — pre-PR-#44
    # this state is realistic: PR #44's bootstrap landed Databento aliases
    # on the exchange-name suffix, and PR #37's IB refresh landed an IB
    # alias with the same string.
    session.add(
        InstrumentAlias(
            id=uuid4(),
            instrument_uid=aapl_ib_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    session.add(
        InstrumentAlias(
            id=uuid4(),
            instrument_uid=aapl_databento_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="databento",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()

    # Act
    svc = MarketHoursService()
    await svc.prime(session, ["AAPL.NASDAQ"])

    # Assert — the IB row's trading_hours wins (NYSE RTH); Databento's
    # NULL is filtered out by the provider gate.
    monday_10am_et = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)  # 10:00 ET
    monday_3am_et = datetime(2026, 4, 27, 7, 0, tzinfo=UTC)  # 03:00 ET
    assert svc.is_in_rth("AAPL.NASDAQ", monday_10am_et) is True, (
        "Should resolve to IB row → 10:00 ET is within NYSE RTH"
    )
    assert svc.is_in_rth("AAPL.NASDAQ", monday_3am_et) is False, (
        "Should resolve to IB row → 03:00 ET is outside NYSE RTH "
        "(if Databento's NULL won, this would fail-open True)"
    )
