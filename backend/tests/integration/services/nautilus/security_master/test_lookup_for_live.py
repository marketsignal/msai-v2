"""Integration tests for :func:`lookup_for_live`.

Follows the per-function ``session_factory`` + module-scoped
``isolated_postgres_url`` fixture pattern from
``test_security_master_resolve_live.py`` — one container per module
(amortizes container start cost), fresh engine + schema per test
(each test gets its own event loop under pytest-asyncio, and asyncpg
connections must not cross event loops).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models.base import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    AmbiguousRegistryError,
    AssetClass,
    RegistryIncompleteError,
    RegistryMissError,
    ResolvedInstrument,
    UnsupportedAssetClassError,
    lookup_for_live,
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


async def _seed_aapl(session: AsyncSession) -> InstrumentDefinition:
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
    return idef


async def test_lookup_bare_ticker_returns_resolved_instrument(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bare input ``'AAPL'`` must resolve via ``find_by_raw_symbol``."""
    async with session_factory() as session:
        await _seed_aapl(session)

        result = await lookup_for_live(
            ["AAPL"],
            as_of_date=date(2026, 4, 20),
            session=session,
        )

        assert len(result) == 1
        ri = result[0]
        assert isinstance(ri, ResolvedInstrument)
        assert ri.canonical_id == "AAPL.NASDAQ"
        assert ri.asset_class == AssetClass.EQUITY
        assert ri.contract_spec["secType"] == "STK"
        assert ri.contract_spec["primaryExchange"] == "NASDAQ"


async def test_lookup_dotted_alias_returns_resolved_instrument(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dotted input ``'AAPL.NASDAQ'`` must resolve via ``find_by_alias``."""
    async with session_factory() as session:
        await _seed_aapl(session)

        result = await lookup_for_live(
            ["AAPL.NASDAQ"],
            as_of_date=date(2026, 4, 20),
            session=session,
        )

        assert len(result) == 1
        assert result[0].canonical_id == "AAPL.NASDAQ"


async def test_lookup_empty_symbols_raises_value_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="empty"):
            await lookup_for_live([], as_of_date=date(2026, 4, 20), session=session)


async def test_lookup_requires_as_of_date_not_datetime(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    async with session_factory() as session:
        with pytest.raises(TypeError, match="date"):
            await lookup_for_live(
                ["AAPL"],
                as_of_date=datetime.now(UTC),  # type: ignore[arg-type]
                session=session,
            )


async def test_lookup_partial_miss_aggregates_all_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Task 4 characterization: when some symbols are in registry but
    others aren't, :class:`RegistryMissError` lists ALL missing, not just
    the first one encountered."""
    async with session_factory() as session:
        await _seed_aapl(session)

        with pytest.raises(RegistryMissError) as excinfo:
            await lookup_for_live(
                ["AAPL", "QQQ", "GBP/USD"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )
        assert set(excinfo.value.symbols) == {"QQQ", "GBP/USD"}
        assert "AAPL" not in excinfo.value.symbols
        assert "msai instruments refresh" in str(excinfo.value)


async def test_lookup_same_day_overlap_raises_ambiguous(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two active aliases with the SAME ``effective_from`` — operator
    data-integrity issue; must raise ``AmbiguousRegistryError(reason=
    SAME_DAY_OVERLAP)``, not silently pick one."""
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
        # Two aliases with the SAME effective_from — ambiguous.
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 20),
            )
        )
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESU6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 20),  # same date = ambiguous
            )
        )
        await session.commit()

        with pytest.raises(AmbiguousRegistryError) as excinfo:
            await lookup_for_live(
                ["ES"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )
        assert excinfo.value.symbol == "ES"
        assert excinfo.value.reason == AmbiguousRegistryError.REASON_SAME_DAY_OVERLAP
        assert set(excinfo.value.conflicts) == {"ESM6.CME", "ESU6.CME"}


# =============================================================================
# Task 5: error paths — expired alias, corrupt row (propagation), unsupported
# =============================================================================


async def test_lookup_expired_alias_is_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Alias with ``effective_to`` in the past is treated as a registry miss."""
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
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESH6.CME",  # March 2026 — expired by April
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2025, 12, 15),
                effective_to=date(2026, 3, 19),  # expired
            )
        )
        await session.commit()

        with pytest.raises(RegistryMissError):
            await lookup_for_live(
                ["ES"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )


async def test_lookup_propagates_incomplete_from_build_spec(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkey-patch ``_build_contract_spec`` to raise
    :class:`RegistryIncompleteError`; ``lookup_for_live`` propagates without
    catching. (DB NOT NULL makes a real corrupt row unreachable — the
    monkey-patch is the test seam.)
    """
    async with session_factory() as session:
        await _seed_aapl(session)

        from msai.services.nautilus.security_master import live_resolver

        def _raising_spec(
            definition: InstrumentDefinition,
            alias: InstrumentAlias,
        ) -> dict[str, object]:
            raise RegistryIncompleteError(
                symbol=definition.raw_symbol,
                missing_field="listing_venue",
            )

        monkeypatch.setattr(live_resolver, "_build_contract_spec", _raising_spec)

        with pytest.raises(RegistryIncompleteError) as excinfo:
            await lookup_for_live(
                ["AAPL"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )
        assert excinfo.value.missing_field == "listing_venue"
        assert excinfo.value.symbol == "AAPL"


async def test_lookup_option_asset_class_raises_unsupported(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Option ``asset_class`` raises :class:`UnsupportedAssetClassError`
    at the resolver boundary (not wired for live yet)."""
    async with session_factory() as session:
        idef = InstrumentDefinition(
            raw_symbol="SPY_CALL_500_20260619",
            listing_venue="CBOE",
            routing_venue="SMART",
            asset_class="option",
            provider="interactive_brokers",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="SPY_CALL_500_20260619.CBOE",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

        with pytest.raises(UnsupportedAssetClassError) as excinfo:
            await lookup_for_live(
                ["SPY_CALL_500_20260619.CBOE"],
                as_of_date=date(2026, 4, 20),
                session=session,
            )
        assert excinfo.value.asset_class == AssetClass.OPTION
        assert excinfo.value.symbol == "SPY_CALL_500_20260619.CBOE"


# =============================================================================
# Task 6: futures-roll boundary — ESM6 → ESU6 on 2026-06-20
# =============================================================================


async def _seed_es_roll_aliases(session: AsyncSession) -> None:
    """Seed ES with two non-overlapping aliases spanning the 2026-06-20 roll."""
    idef = InstrumentDefinition(
        raw_symbol="ES",
        listing_venue="CME",
        routing_venue="CME",
        asset_class="futures",
        provider="interactive_brokers",
    )
    session.add(idef)
    await session.flush()
    # ESM6 active until 2026-06-20 exclusive (June contract).
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="ESM6.CME",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 3, 20),
            effective_to=date(2026, 6, 20),  # exclusive: 2026-06-20 is already ESU6
        )
    )
    # ESU6 active from 2026-06-20 (September contract).
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="ESU6.CME",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 6, 20),
        )
    )
    await session.commit()


async def test_lookup_futures_roll_pre_roll_returns_front_month(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pre-roll date (2026-06-19) resolves to ESM6 (June front-month)."""
    async with session_factory() as session:
        await _seed_es_roll_aliases(session)

        result = await lookup_for_live(
            ["ES"],
            as_of_date=date(2026, 6, 19),
            session=session,
        )

        assert len(result) == 1
        assert result[0].canonical_id == "ESM6.CME"
        assert result[0].contract_spec["lastTradeDateOrContractMonth"] == "202606"


async def test_lookup_futures_roll_post_roll_returns_next_month(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Post-roll date (2026-06-20) resolves to ESU6 (September front-month)."""
    async with session_factory() as session:
        await _seed_es_roll_aliases(session)

        result = await lookup_for_live(
            ["ES"],
            as_of_date=date(2026, 6, 20),
            session=session,
        )

        assert len(result) == 1
        assert result[0].canonical_id == "ESU6.CME"
        assert result[0].contract_spec["lastTradeDateOrContractMonth"] == "202609"
