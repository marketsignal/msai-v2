"""Integration tests for lookup_for_live structured-log emission.

The project renders structlog events directly (bypassing stdlib), so
`caplog` cannot see the structured kwargs. We use
`structlog.testing.capture_logs` which captures the events as a list
of dicts regardless of the configured processor pipeline.

Fixtures mirror test_lookup_for_live.py's per-function session_factory
pattern (module-scoped Postgres container, per-function engine).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import structlog.testing
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    RegistryMissError,
    lookup_for_live,
)

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
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(delete(InstrumentAlias))
        await conn.execute(delete(InstrumentDefinition))
    await engine.dispose()


async def _seed_aapl(session: AsyncSession) -> InstrumentDefinition:
    d = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        provider="interactive_brokers",
    )
    session.add(d)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=d.instrument_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()
    return d


async def test_successful_resolution_emits_structured_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_aapl(session)
        with structlog.testing.capture_logs() as captured:
            await lookup_for_live(
                ["AAPL"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )

    events = [e for e in captured if e.get("source") == "registry"]
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "live_instrument_resolved"
    assert evt["symbol"] == "AAPL"
    assert evt["canonical_id"] == "AAPL.NASDAQ"
    assert evt["asset_class"] == "equity"
    assert evt["as_of_date"] == "2026-04-20"
    assert evt["log_level"] == "info"


async def test_registry_miss_emits_structured_log_per_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with structlog.testing.capture_logs() as captured:
            with pytest.raises(RegistryMissError):
                await lookup_for_live(
                    ["UNKNOWN_A", "UNKNOWN_B"],
                    as_of_date=date(2026, 4, 20),
                    session=session,
                )

    events = [e for e in captured if e.get("source") == "registry_miss"]
    assert len(events) == 2
    symbols = {e["symbol"] for e in events}
    assert symbols == {"UNKNOWN_A", "UNKNOWN_B"}
    assert all(e["log_level"] == "warning" for e in events)
