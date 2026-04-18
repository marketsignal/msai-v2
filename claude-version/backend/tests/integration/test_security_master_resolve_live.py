"""Integration tests for :meth:`SecurityMaster.resolve_for_live`.

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

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
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


@pytest.mark.asyncio
async def test_resolve_for_live_cold_miss_outside_closed_universe_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Phase-1 closed universe: AAPL/MSFT/SPY/EUR/USD/ES only.

    ``resolve_for_live`` for any other symbol must raise ``ValueError``
    from :func:`canonical_instrument_id`'s closed-universe guard — and
    must never reach the IB qualifier (``mock_qualifier.qualify`` stays
    uncalled).
    """
    async with session_factory() as session:
        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock()
        sm = SecurityMaster(qualifier=mock_qualifier, db=session)
        with pytest.raises(ValueError, match="GOOG|closed universe|Unknown"):
            await sm.resolve_for_live(["GOOG"])
        mock_qualifier.qualify.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_for_live_concurrent_cold_miss_is_race_safe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent ``resolve_for_live("MSFT")`` calls from fresh
    sessions with an empty registry — both should succeed and exactly
    ONE :class:`InstrumentDefinition` row should exist afterwards.

    Covers the race closed by F2's ``INSERT ... ON CONFLICT DO UPDATE``
    rewrite of ``_upsert_definition_and_alias`` — the old
    SELECT-then-INSERT path would intermittently raise ``IntegrityError``
    on the ``uq_instrument_definitions_symbol_provider_asset`` constraint.

    We use a real Nautilus ``Equity`` instrument as the qualifier's
    return value so either concurrent path can cache-HIT the other's
    write and deserialize the JSONB row back into a valid Nautilus
    instrument via ``_instrument_from_cache_row``.
    """
    import asyncio

    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    real_instrument = TestInstrumentProvider.equity(symbol="MSFT", venue="NASDAQ")

    async def _resolve_in_session() -> list[str]:
        async with session_factory() as session:
            mock_qualifier = MagicMock()
            mock_qualifier.qualify = AsyncMock(return_value=real_instrument)
            mock_provider = MagicMock()
            mock_provider.contract_details = {}
            mock_qualifier._provider = mock_provider
            sm = SecurityMaster(qualifier=mock_qualifier, db=session)
            result = await sm.resolve_for_live(["MSFT"])
            # resolve_for_live flushes but does not commit — commit here so
            # the second concurrent session (and the final count query) can
            # observe the upsert.
            await session.commit()
            return result

    # Two concurrent cold-miss resolves — both should succeed under
    # ON CONFLICT DO UPDATE semantics.
    results = await asyncio.gather(_resolve_in_session(), _resolve_in_session())
    assert results == [["MSFT.NASDAQ"], ["MSFT.NASDAQ"]]

    # Verify exactly ONE InstrumentDefinition row exists — the second
    # upsert collapsed onto the first via the unique constraint.
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(InstrumentDefinition).where(
                        InstrumentDefinition.raw_symbol == "MSFT"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"


@pytest.mark.asyncio
async def test_resolve_for_live_closes_prior_active_alias_on_new_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Upserting a new alias for an existing ``(raw_symbol, provider)``
    must close any prior active alias (``effective_to IS NULL``) so the
    ``[effective_from, effective_to)`` windows stay non-overlapping.

    Scenario: ``AAPL`` exists under provider=interactive_brokers with an
    active alias ``AAPL.NASDAQ``. A refresh / venue change causes the
    qualifier to return a new canonical ``AAPL.ARCA``. After the upsert,
    the old alias MUST have ``effective_to=today`` and the new one MUST
    be active — exactly one active alias per ``(instrument_uid, provider)``
    at any point.
    """
    async with session_factory() as session:
        # Arrange — seed AAPL + active AAPL.NASDAQ alias.
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

        sm = SecurityMaster(qualifier=MagicMock(), db=session)

        # Act — directly invoke the upsert helper with a new alias.
        # (The helper is the subject of the fix; exercising it directly
        # avoids the warm-path short-circuit in ``resolve_for_live``.)
        await sm._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="ARCA",
            routing_venue="ARCA",
            asset_class="equity",
            alias_string="AAPL.ARCA",
        )
        await session.commit()

        # Assert — old alias closed today, new alias active.
        today = datetime.now(UTC).date()
        old_alias = (
            await session.execute(
                select(InstrumentAlias).where(
                    InstrumentAlias.alias_string == "AAPL.NASDAQ",
                    InstrumentAlias.provider == "interactive_brokers",
                )
            )
        ).scalar_one()
        assert old_alias.effective_to == today

        new_alias = (
            await session.execute(
                select(InstrumentAlias).where(
                    InstrumentAlias.alias_string == "AAPL.ARCA",
                    InstrumentAlias.provider == "interactive_brokers",
                )
            )
        ).scalar_one()
        assert new_alias.effective_to is None
        assert new_alias.effective_from == today

        # Invariant — exactly one active alias for this (instrument_uid,
        # provider) pair.
        active_count = len(
            (
                await session.execute(
                    select(InstrumentAlias).where(
                        InstrumentAlias.instrument_uid == idef.instrument_uid,
                        InstrumentAlias.provider == "interactive_brokers",
                        InstrumentAlias.effective_to.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert active_count == 1
