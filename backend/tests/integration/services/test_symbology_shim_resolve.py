"""Integration tests for :func:`resolve_for_databento`.

Follows the per-function ``session_factory`` + module-scoped
``isolated_postgres_url`` fixture pattern from
``tests/integration/services/nautilus/security_master/test_lookup_for_live.py`` —
one container per module (amortizes container start cost), fresh engine +
schema per test (each test gets its own event loop under pytest-asyncio,
and asyncpg connections must not cross event loops).

The async outbound path needs a real registry session because it delegates
to :func:`lookup_for_live`; trying to mock the resolver would re-implement
all of its dispatch logic. Pure / sync coverage lives in
``tests/unit/services/test_symbology_shim.py``.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from nautilus_trader.model.identifiers import InstrumentId
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.symbology_shim import (
    DatabentoSubscriptionTarget,
    resolve_for_databento,
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


async def _seed_aapl_equity(session: AsyncSession) -> None:
    """Seed AAPL as an equity with a single active IB alias."""
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


async def _seed_es_future(session: AsyncSession) -> None:
    """Seed ES as a futures contract with an active ESM6 alias."""
    idef = InstrumentDefinition(
        raw_symbol="ES",
        listing_venue="CME",
        routing_venue="CME",
        asset_class="futures",
        provider="interactive_brokers",
    )
    session.add(idef)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="ESM6.CME",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 3, 20),
            effective_to=date(2026, 6, 20),
        )
    )
    await session.commit()


async def test_outbound_resolves_aapl_to_dbeq_basic_xnas(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AAPL.IBKR canonical → Databento ``DBEQ.BASIC`` / XNAS / ``AAPL``."""
    async with session_factory() as session:
        await _seed_aapl_equity(session)

        target = await resolve_for_databento(
            InstrumentId.from_str("AAPL.IBKR"),
            session=session,
            as_of_date=date(2026, 5, 29),
        )

        assert isinstance(target, DatabentoSubscriptionTarget)
        assert target.dataset == "DBEQ.BASIC"
        assert target.native_symbol == "AAPL"
        assert target.native_venue == "XNAS"


async def test_outbound_futures_raises_not_implemented(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PR 1 scope is equities-only (council 2026-05-29).

    The shim must raise ``NotImplementedError`` for futures so callers
    fail fast and can't silently subscribe to the wrong contract via the
    ambiguous ``contract_spec['symbol']`` root.
    """
    async with session_factory() as session:
        await _seed_es_future(session)

        with pytest.raises(NotImplementedError, match="equities only"):
            await resolve_for_databento(
                InstrumentId.from_str("ES.IBKR"),
                session=session,
                as_of_date=date(2026, 5, 1),  # within ESM6 active window
            )


async def test_outbound_registry_miss_propagates_value_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unseeded symbol → resolver raises ``RegistryMissError`` (subclass of
    ``ValueError``). The shim propagates it unchanged so supervisor's
    permanent-catch wiring still fires."""
    async with session_factory() as session:
        # No seed — registry is empty.

        with pytest.raises(ValueError, match="not in registry"):
            await resolve_for_databento(
                InstrumentId.from_str("NOPE.IBKR"),
                session=session,
                as_of_date=date(2026, 5, 29),
            )


# ---------------------------------------------------------------------------
# Codex iter 3 F2 — derive Databento native venue from LISTING venue
# (not hardcoded XNAS for every equity).
# ---------------------------------------------------------------------------


async def _seed_equity(
    session: AsyncSession,
    *,
    raw_symbol: str,
    listing_venue: str,
) -> None:
    """Seed an equity with the given listing venue + an active IB alias."""
    idef = InstrumentDefinition(
        raw_symbol=raw_symbol,
        listing_venue=listing_venue,
        routing_venue="SMART",
        asset_class="equity",
        provider="interactive_brokers",
    )
    session.add(idef)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string=f"{raw_symbol}.{listing_venue}",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
        )
    )
    await session.commit()


@pytest.mark.parametrize(
    ("raw_symbol", "listing_venue", "expected_native_venue"),
    [
        ("AAPL", "NASDAQ", "XNAS"),
        ("GE", "NYSE", "XNYS"),
        ("SPY", "ARCA", "ARCX"),
        ("GLD", "AMEX", "XASE"),
    ],
)
async def test_outbound_resolves_listing_venue_to_databento_native(
    session_factory: async_sessionmaker[AsyncSession],
    raw_symbol: str,
    listing_venue: str,
    expected_native_venue: str,
) -> None:
    """F2: the Databento ``native_venue`` MUST be derived from the
    RESOLVED listing venue (``contract_spec['primaryExchange']``), not
    hardcoded to ``XNAS`` for every equity. The four US listing venues
    in ``_LISTING_VENUE_TO_DATABENTO_NATIVE`` each produce the right
    native suffix so the supervisor's ``venue_dataset_map`` keys agree
    with what Databento itself emits."""
    async with session_factory() as session:
        await _seed_equity(session, raw_symbol=raw_symbol, listing_venue=listing_venue)

        target = await resolve_for_databento(
            InstrumentId.from_str(f"{raw_symbol}.IBKR"),
            session=session,
            as_of_date=date(2026, 5, 29),
        )

        # All four listing venues currently map to the same Databento
        # BASIC bundle; only the native_venue varies. native_symbol is
        # the operator-typed raw symbol.
        assert target.dataset == "DBEQ.BASIC"
        assert target.native_symbol == raw_symbol
        assert target.native_venue == expected_native_venue


async def test_outbound_unknown_listing_venue_raises_unsupported(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter 19 P2: a listing venue not in the mapping table
    (e.g. OTC, BATS, a future international listing) raises the
    distinct ``UnsupportedListingVenueError`` so the supervisor's
    ``(NotImplementedError, LiveResolverError)`` per-account fallback
    catch CAN'T swallow it — the deployment fails loud with
    ``SPAWN_FAILED_PERMANENT`` instead of silently downgrading to the
    legacy IB-data topology.

    The four US venues NASDAQ / NYSE / ARCA / AMEX are the only ones
    PR 1 supports. Extending the support list is an explicit edit to
    ``_LISTING_VENUE_TO_DATABENTO_NATIVE`` — not a silent fallback.
    """
    from msai.services.symbology_shim import UnsupportedListingVenueError

    async with session_factory() as session:
        # Seed a hypothetical BATS-listed equity (not currently in the
        # F2 mapping table).
        await _seed_equity(session, raw_symbol="WEIRD", listing_venue="BATS")

        with pytest.raises(
            UnsupportedListingVenueError,
            match="no Databento native-venue mapping",
        ):
            await resolve_for_databento(
                InstrumentId.from_str("WEIRD.IBKR"),
                session=session,
                as_of_date=date(2026, 5, 29),
            )
