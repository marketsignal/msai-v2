"""Integration tests for :class:`InstrumentRegistry`.

Exercises the alias / raw-symbol lookup layer over
``instrument_definitions`` + ``instrument_aliases`` against a real Postgres
container.

Follows the per-module ``session_factory`` pattern from
``test_instrument_definition_crud.py`` — no shared fixture exists.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.registry import (
    AmbiguousSymbolError,
    InstrumentRegistry,
    RegistryDefinitionNotFoundError,
)

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
async def test_find_by_alias_honors_as_of_date(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
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
        # Expired March contract
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESH6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2025, 12, 19),
                effective_to=date(2026, 3, 18),
            )
        )
        # Current June contract
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 18),
            )
        )
        await session.commit()

        registry = InstrumentRegistry(session)
        # As-of mid-Feb: March contract is active
        result_feb = await registry.find_by_alias(
            "ESH6.CME", provider="interactive_brokers", as_of_date=date(2026, 2, 15)
        )
        assert result_feb is not None
        # As-of mid-April: March contract is expired
        result_apr = await registry.find_by_alias(
            "ESH6.CME", provider="interactive_brokers", as_of_date=date(2026, 4, 15)
        )
        assert result_apr is None


@pytest.mark.asyncio
async def test_find_by_raw_symbol_requires_provider(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Schema uniqueness is (raw_symbol, provider, asset_class). Cross-provider
    ambiguity is by design — callers must specify provider. This test proves
    both providers coexist as distinct rows and are retrievable independently."""
    async with session_factory() as session:
        session.add(
            InstrumentDefinition(
                raw_symbol="XYZ",
                listing_venue="NASDAQ",
                routing_venue="NASDAQ",
                asset_class="equity",
                provider="interactive_brokers",
            )
        )
        session.add(
            InstrumentDefinition(
                raw_symbol="XYZ",
                listing_venue="NASDAQ",
                routing_venue="NASDAQ",
                asset_class="equity",
                provider="databento",
            )
        )
        await session.commit()

        registry = InstrumentRegistry(session)
        ib_row = await registry.find_by_raw_symbol("XYZ", provider="interactive_brokers")
        db_row = await registry.find_by_raw_symbol("XYZ", provider="databento")
        assert ib_row is not None
        assert db_row is not None
        assert ib_row.provider == "interactive_brokers"
        assert db_row.provider == "databento"


@pytest.mark.asyncio
async def test_require_definition_raises_on_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        registry = InstrumentRegistry(session)
        with pytest.raises(RegistryDefinitionNotFoundError):
            await registry.require_definition(
                "ZZZZ.NASDAQ",
                provider="interactive_brokers",
                as_of_date=date(2026, 4, 20),
            )


@pytest.mark.asyncio
async def test_find_by_raw_symbol_raises_on_ambiguous_asset_classes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Schema allows multiple rows per (raw_symbol, provider) across
    asset_classes. Without asset_class, resolver must refuse rather than
    silently pick."""
    async with session_factory() as session:
        # Seed SPY as equity AND as option-underlying under same provider
        for ac in ("equity", "option"):
            session.add(
                InstrumentDefinition(
                    raw_symbol="SPY",
                    listing_venue="NASDAQ",
                    routing_venue="NASDAQ",
                    asset_class=ac,
                    provider="databento",
                )
            )
        await session.commit()

        registry = InstrumentRegistry(session)
        with pytest.raises(AmbiguousSymbolError, match="SPY"):
            await registry.find_by_raw_symbol("SPY", provider="databento")

        # Specifying asset_class resolves unambiguously
        result_equity = await registry.find_by_raw_symbol(
            "SPY", provider="databento", asset_class="equity"
        )
        assert result_equity is not None
        assert result_equity.asset_class == "equity"
