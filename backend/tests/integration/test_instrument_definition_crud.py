"""Integration tests for :class:`InstrumentDefinition` + :class:`InstrumentAlias`.

Exercises the real DB constraints end-to-end against a Postgres container:

* CRUD round-trip with ``ON DELETE CASCADE`` on aliases
* ``uq_instrument_aliases_string_provider_from`` unique constraint
* ``ck_instrument_definitions_asset_class`` CHECK constraint
* ``ck_instrument_definitions_continuous_pattern_shape`` regex CHECK

Follows the per-module ``session_factory`` pattern from
``test_instrument_cache_model.py`` — no shared fixture exists.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentAlias, InstrumentDefinition

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
async def test_crud_roundtrip_with_cascade_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert definition + alias, delete definition, verify the alias is
    gone via ``ON DELETE CASCADE``."""
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="interactive_brokers",
            roll_policy="third_friday_quarterly",
        )
        session.add(idef)
        await session.flush()
        uid = idef.instrument_uid
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 17),
            )
        )
        await session.commit()

    async with session_factory() as session:
        reloaded = await session.get(InstrumentDefinition, uid)
        assert reloaded is not None
        await session.delete(reloaded)
        await session.commit()

    async with session_factory() as session:
        aliases = (
            (
                await session.execute(
                    select(InstrumentAlias).where(InstrumentAlias.instrument_uid == uid)
                )
            )
            .scalars()
            .all()
        )
        assert aliases == []


@pytest.mark.asyncio
async def test_unique_alias_per_provider_per_effective_from(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``uq_instrument_aliases_string_provider_from`` rejects the same
    ``(alias_string, provider, effective_from)`` tuple twice."""
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="interactive_brokers",
        )
        session.add(idef)
        await session.flush()
        uid = idef.instrument_uid
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_asset_class_check_rejects_invalid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ck_instrument_definitions_asset_class`` only allows the
    enumerated asset classes — ``bond`` is not one of them."""
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="X",
                listing_venue="Y",
                routing_venue="Y",
                asset_class="bond",
                provider="interactive_brokers",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_effective_window_check_rejects_inverted_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ck_instrument_aliases_effective_window`` rejects an alias row whose
    ``effective_to`` is strictly BEFORE ``effective_from``. ``NULL`` (active
    alias) and zero-width ``effective_to == effective_from`` (same-day
    rotation audit row) are both allowed.
    """
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="interactive_brokers",
        )
        session.add(idef)
        await session.flush()
        uid = idef.instrument_uid
        # Active alias with effective_to=None — allowed.
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 17),
                effective_to=None,
            )
        )
        await session.commit()

    async with session_factory() as session:
        # Zero-width window — effective_to == effective_from — allowed.
        # Represents a same-day rotation where an alias was superseded on
        # the same calendar day it was seeded; semantically the half-open
        # interval [F, F) contains no dates, so the row is never active.
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="ESU6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 6, 1),
            )
        )
        await session.commit()

    async with session_factory() as session:
        # Strictly inverted window — effective_to < effective_from — must fail.
        session.add(
            InstrumentAlias(
                instrument_uid=uid,
                alias_string="ESZ6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 12, 1),
                effective_to=date(2026, 11, 30),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_continuous_pattern_check_rejects_invalid_shape(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ck_instrument_definitions_continuous_pattern_shape`` accepts the
    Databento continuous shape ``.Z.5`` but rejects a bare ``Z5``."""
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="ES",
                listing_venue="CME",
                routing_venue="CME",
                asset_class="futures",
                provider="databento",
                continuous_pattern=".Z.5",
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="NQ",
                listing_venue="CME",
                routing_venue="CME",
                asset_class="futures",
                provider="databento",
                continuous_pattern="Z5",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
