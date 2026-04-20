"""Integration test: supervisor's payload factory threads
:class:`ResolvedInstrument` through :class:`StrategyMemberPayload`.

Seeds AAPL in the registry + invokes :func:`lookup_for_live` directly
(without spawning a real subprocess), then verifies the field flows
through :class:`StrategyMemberPayload`. Asserts:

1. ``lookup_for_live`` returns a :class:`ResolvedInstrument` for a
   seeded symbol.
2. :class:`StrategyMemberPayload` accepts ``resolved_instruments=(...)``
   kwarg and preserves it.
3. :class:`StrategyMemberPayload` defaults ``resolved_instruments`` to
   an empty tuple — existing construction sites are backward-compatible.
4. An un-seeded symbol raises :class:`RegistryMissError` (which
   ``ProcessManager`` would catch and classify — this test only
   verifies the raise).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    RegistryMissError,
    ResolvedInstrument,
    lookup_for_live,
)
from msai.services.nautilus.trading_node_subprocess import (
    StrategyMemberPayload,
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


async def test_lookup_for_live_returns_resolved_for_seeded_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Direct test of :func:`lookup_for_live` in the integration
    environment that the supervisor uses — confirms the resolver works
    against a real Postgres + SQLAlchemy async session stack.
    """
    # Arrange
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            provider="interactive_brokers",
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

        # Act
        resolved = await lookup_for_live(
            ["AAPL"],
            as_of_date=date(2026, 4, 20),
            session=session,
        )

    # Assert
    assert len(resolved) == 1
    assert isinstance(resolved[0], ResolvedInstrument)
    assert resolved[0].canonical_id == "AAPL.NASDAQ"
    assert resolved[0].asset_class is AssetClass.EQUITY
    # Tuple-friendly: tuple() construction preserves order
    as_tuple = tuple(resolved)
    assert len(as_tuple) == 1


async def test_strategy_member_payload_carries_resolved_instruments() -> None:
    """:class:`StrategyMemberPayload` accepts
    ``resolved_instruments=(...)`` kwarg and preserves it."""
    # Arrange
    resolved = (
        ResolvedInstrument(
            canonical_id="AAPL.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK",
                "symbol": "AAPL",
                "exchange": "SMART",
                "primaryExchange": "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    )

    # Act
    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.buy_hold:BuyHold",
        strategy_config_path="strategies.example.buy_hold:BuyHoldConfig",
        strategy_config={},
        strategy_code_hash="abc123",
        strategy_id_full="bh-001",
        instruments=["AAPL"],
        resolved_instruments=resolved,
    )

    # Assert
    assert member.resolved_instruments == resolved
    assert member.resolved_instruments[0].canonical_id == "AAPL.NASDAQ"
    assert member.resolved_instruments[0].asset_class is AssetClass.EQUITY


async def test_strategy_member_payload_default_resolved_is_empty_tuple() -> None:
    """Existing test call sites that don't pass ``resolved_instruments``
    still work — the field defaults to an empty tuple."""
    # Arrange / Act
    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.buy_hold:BuyHold",
        strategy_config_path="strategies.example.buy_hold:BuyHoldConfig",
        strategy_config={},
        strategy_code_hash="abc123",
        strategy_id_full="bh-001",
        instruments=["AAPL"],
    )

    # Assert
    assert member.resolved_instruments == ()


async def test_lookup_for_live_raises_miss_for_unseeded_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An un-seeded symbol must raise :class:`RegistryMissError` — the
    supervisor's ``ProcessManager`` would then catch and classify to
    :attr:`FailureKind.REGISTRY_MISS`. This test verifies only the
    raise, not the supervisor dispatch (see unit-test for that).
    """
    async with session_factory() as session:
        # Act + Assert
        with pytest.raises(RegistryMissError) as excinfo:
            await lookup_for_live(
                ["UNSEEDED"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )

    assert "UNSEEDED" in excinfo.value.symbols
    assert excinfo.value.as_of_date == date(2026, 4, 20)
