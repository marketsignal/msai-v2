"""Integration tests for :meth:`SecurityMaster.resolve_for_live` (Task 8).

Exercises the warm / cold paths of the live-trading resolve entrypoint:

- Warm hit via :meth:`InstrumentRegistry.find_by_alias` (dotted input) —
  must NOT invoke the IB qualifier.
- Cold miss — fall through to ``canonical_instrument_id`` +
  ``self.resolve(spec)`` + ``_upsert_definition_and_alias``.

Follows the per-module ``session_factory`` + ``isolated_postgres_url``
fixture pattern from ``test_instrument_registry.py`` /
``test_security_master.py``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.service import SecurityMaster

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
async def test_resolve_for_live_warm_hit_does_not_call_ib(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dotted alias already in registry → return as-is, no IB call."""
    async with session_factory() as session:
        # Arrange — seed the registry with an active AAPL.NASDAQ alias
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="NASDAQ",
            asset_class="equity",
            provider="interactive_brokers",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock()
        sm = SecurityMaster(qualifier=mock_qualifier, db=session)

        # Act
        ids = await sm.resolve_for_live(["AAPL.NASDAQ"])

        # Assert
        assert ids == ["AAPL.NASDAQ"]
        mock_qualifier.qualify.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_for_live_cold_miss_calls_ib_and_upserts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty registry + bare symbol → delegate to canonical_instrument_id +
    self.resolve(spec) → upsert definition+alias → return canonical id.
    """
    async with session_factory() as session:
        # Arrange — fake Nautilus instrument returned by the qualifier
        fake_instrument = MagicMock()
        fake_instrument.id = MagicMock()
        fake_instrument.id.__str__ = MagicMock(return_value="MSFT.NASDAQ")
        fake_instrument.id.venue.value = "NASDAQ"
        fake_instrument.raw_symbol.value = "MSFT"
        # Need a real class name so `_asset_class_for_instrument` returns
        # "equity" (Equity → equity). MagicMock's default __class__.__name__
        # is "MagicMock" which also falls through to "equity", but we pin
        # it explicitly to avoid coupling to that default.
        fake_instrument.__class__.__name__ = "Equity"
        # ``SecurityMaster._write_cache`` (via ``self.resolve``) serializes
        # the instrument through ``nautilus_instrument_to_cache_json`` which
        # calls ``instrument.to_dict(instrument)``. Return a JSONB-safe dict.
        fake_instrument.to_dict = MagicMock(
            return_value={
                "type": "Equity",
                "instrument_id": "MSFT.NASDAQ",
                "raw_symbol": "MSFT",
            }
        )

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock(return_value=fake_instrument)

        mock_provider = MagicMock()
        fake_details = MagicMock()
        fake_details.contract.primaryExchange = "NASDAQ"
        mock_provider.contract_details = {fake_instrument.id: fake_details}
        mock_qualifier._provider = mock_provider

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)

        # Act
        ids = await sm.resolve_for_live(["MSFT"])

        # Assert — return value
        assert ids == ["MSFT.NASDAQ"]
        mock_qualifier.qualify.assert_awaited_once()

        # Assert — definition + alias were upserted
        from sqlalchemy import select

        idef_row = (
            await session.execute(
                select(InstrumentDefinition).where(
                    InstrumentDefinition.raw_symbol == "MSFT",
                    InstrumentDefinition.provider == "interactive_brokers",
                )
            )
        ).scalar_one()
        assert idef_row.listing_venue == "NASDAQ"
        assert idef_row.routing_venue == "NASDAQ"
        assert idef_row.asset_class == "equity"
        assert idef_row.lifecycle_state == "active"

        alias_row = (
            await session.execute(
                select(InstrumentAlias).where(
                    InstrumentAlias.alias_string == "MSFT.NASDAQ",
                    InstrumentAlias.provider == "interactive_brokers",
                )
            )
        ).scalar_one()
        assert alias_row.venue_format == "exchange_name"
        assert alias_row.effective_to is None
