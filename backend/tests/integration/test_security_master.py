"""Integration tests for :class:`SecurityMaster`.

Uses a testcontainer Postgres for the registry layer and a stub
qualifier for the IB side. Does NOT touch a real IB connection.

Cache-layer tests (legacy ``InstrumentCache``-backed paths) were removed
when ``resolve``/``bulk_resolve`` were rewired to be registry-only and
the ``instrument_cache`` table was dropped.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.nautilus.security_master.specs import InstrumentSpec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Registry-only resolve / bulk_resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_with_registry_warm_hit_does_not_call_qualifier(
    session: AsyncSession,
) -> None:
    """resolve(spec) should NOT call IBQualifier when the registry has
    an active alias for the spec's canonical_id."""
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
            trading_hours={"timezone": "America/New_York", "rth": [], "eth": []},
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

    qualifier = AsyncMock()
    qualifier.qualify = AsyncMock(side_effect=AssertionError("qualifier should not be called"))

    sm = SecurityMaster(qualifier=qualifier, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")

    instrument = await sm.resolve(spec)

    assert str(instrument.id) == "AAPL.NASDAQ"
    qualifier.qualify.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_cold_miss_qualifies_and_upserts_registry(
    session: AsyncSession,
) -> None:
    """resolve(spec) on registry miss → qualify via IB → upsert registry → return."""
    from nautilus_trader.test_kit.providers import (  # noqa: PLC0415
        TestInstrumentProvider as _TestProv,
    )

    from msai.services.nautilus.security_master.registry import (  # noqa: PLC0415
        InstrumentRegistry as _Registry,
    )

    qualifier = AsyncMock()
    fake_aapl = _TestProv.equity(symbol="AAPL", venue="NASDAQ")
    qualifier.qualify = AsyncMock(return_value=fake_aapl)
    qualifier._provider = MagicMock(contract_details={})
    # ``listing_venue_for`` is a sync helper now — wire a real-shaped return
    # value so the AsyncMock auto-spec doesn't return a coroutine that lands
    # in the SQL parameter list.
    qualifier.listing_venue_for = MagicMock(return_value="NASDAQ")

    sm = SecurityMaster(qualifier=qualifier, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")

    instrument = await sm.resolve(spec)
    await session.commit()

    qualifier.qualify.assert_called_once_with(spec)
    assert str(instrument.id) == "AAPL.NASDAQ"
    registry = _Registry(session)
    # The upsert stamps ``effective_from = datetime.now(UTC).date()``; query
    # at that same UTC date so the alias-window predicate
    # (``effective_from <= as_of_date``) holds even when this test is run
    # during the local-vs-UTC midnight gap.
    from datetime import UTC as _UTC  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    today_utc = _dt.now(_UTC).date()
    found = await registry.find_by_alias(
        "AAPL.NASDAQ", provider="interactive_brokers", as_of_date=today_utc
    )
    assert found is not None


@pytest.mark.asyncio
async def test_resolve_cold_miss_without_qualifier_raises(session: AsyncSession) -> None:
    sm = SecurityMaster(qualifier=None, db=session)
    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
    with pytest.raises(ValueError, match="requires an IBQualifier"):
        await sm.resolve(spec)


@pytest.mark.asyncio
async def test_bulk_resolve_empty_input_returns_empty_list(
    session: AsyncSession,
) -> None:
    master = SecurityMaster(qualifier=AsyncMock(), db=session)
    assert await master.bulk_resolve([]) == []


@pytest.mark.asyncio
async def test_bulk_resolve_one_select_for_warm_batch(session: AsyncSession) -> None:
    """bulk_resolve issues one SELECT for warm-hit aliases and never
    calls the qualifier when every spec is warm."""
    for raw in ("AAPL", "MSFT"):
        uid = uuid4()
        session.add(
            InstrumentDefinition(
                instrument_uid=uid,
                raw_symbol=raw,
                provider="interactive_brokers",
                asset_class="equity",
                listing_venue="NASDAQ",
                routing_venue="SMART",
                lifecycle_state="active",
            )
        )
        session.add(
            InstrumentAlias(
                id=uuid4(),
                instrument_uid=uid,
                alias_string=f"{raw}.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
    await session.commit()

    qualifier = AsyncMock()
    qualifier.qualify = AsyncMock(side_effect=AssertionError("not called for warm hits"))

    sm = SecurityMaster(qualifier=qualifier, db=session)
    specs = [
        InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
        InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),
    ]
    results = await sm.bulk_resolve(specs)
    assert [str(r.id) for r in results] == ["AAPL.NASDAQ", "MSFT.NASDAQ"]
    qualifier.qualify.assert_not_called()


# ---------------------------------------------------------------------------
# bulk_resolve — warm-hit on non-equity-fx asset_class delegates to qualifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_resolve_warm_hit_futures_delegates_to_qualifier(
    session: AsyncSession,
) -> None:
    """A warm hit on a futures definition cannot be served from spec alone
    (``_build_instrument_from_spec`` only supports equity / forex). The
    resolver MUST delegate to the IB qualifier so the IB provider IS the
    runtime source of truth for futures contract details — the registry
    row only confirms the alias is operator-blessed, not the full
    instrument shape."""
    from datetime import date as _date  # noqa: PLC0415

    from nautilus_trader.test_kit.providers import (  # noqa: PLC0415
        TestInstrumentProvider as _TestProv,
    )

    es_uid = uuid4()
    session.add(
        InstrumentDefinition(
            instrument_uid=es_uid,
            raw_symbol="ES",
            provider="interactive_brokers",
            asset_class="futures",
            listing_venue="CME",
            routing_venue="CME",
            lifecycle_state="active",
        )
    )
    session.add(
        InstrumentAlias(
            id=uuid4(),
            instrument_uid=es_uid,
            alias_string="ESM6.CME",
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=_date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()

    qualifier = AsyncMock()
    fake_es = _TestProv.equity(symbol="ES", venue="CME")  # shape-only stand-in
    qualifier.qualify = AsyncMock(return_value=fake_es)
    qualifier._provider = MagicMock(contract_details={})
    qualifier.listing_venue_for = MagicMock(return_value="CME")

    sm = SecurityMaster(qualifier=qualifier, db=session)
    spec = InstrumentSpec(
        asset_class="future",
        symbol="ES",
        venue="CME",
        expiry=date(2026, 6, 19),
    )

    results = await sm.bulk_resolve([spec])
    await session.commit()

    # Critical: warm-hit-futures DELEGATED to qualifier (not crashed via
    # ``_build_instrument_from_spec`` NotImplementedError).
    qualifier.qualify.assert_called_once_with(spec)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# _build_instrument_from_spec NotImplementedError for unsupported asset classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asset_class", "kwargs"),
    [
        (
            "future",
            {"symbol": "ES", "venue": "CME", "expiry": date(2026, 6, 19)},
        ),
        (
            "option",
            {
                "symbol": "AAPL",
                "venue": "SMART",
                "expiry": date(2026, 6, 19),
                "strike": __import__("decimal").Decimal("150"),
                "right": "C",
                "underlying": "AAPL",
            },
        ),
        ("index", {"symbol": "^SPX", "venue": "CBOE"}),
    ],
)
@pytest.mark.asyncio
async def test_build_instrument_from_spec_raises_for_unsupported_asset_class(
    session: AsyncSession,
    asset_class: str,
    kwargs: dict[str, object],
) -> None:
    """v1 spec-build covers equity + forex only. Future / option / index
    must raise :class:`NotImplementedError` pointing operators at
    ``live_resolver.lookup_for_live`` — that's the canonical primitive
    when a Nautilus :class:`Instrument` for those asset classes is needed
    without a live IB connection."""
    sm = SecurityMaster(qualifier=AsyncMock(), db=session)
    spec = InstrumentSpec(asset_class=asset_class, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(NotImplementedError, match=r"lookup_for_live"):
        sm._build_instrument_from_spec(spec)


# ---------------------------------------------------------------------------
# _upsert_definition_and_alias — trading_hours COALESCE 4-cell matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_hours", "incoming_hours", "expected_hours"),
    [
        # Existing NULL + new populated → UPDATE to new
        (None, {"timezone": "America/New_York"}, {"timezone": "America/New_York"}),
        # Existing populated + new NULL → KEEP existing (the COALESCE rationale —
        # a writer without IB contract details must NOT clobber prior data).
        (
            {"timezone": "America/New_York"},
            None,
            {"timezone": "America/New_York"},
        ),
        # Both populated → new wins (excluded.trading_hours overrides COALESCE).
        (
            {"timezone": "America/New_York"},
            {"timezone": "America/Chicago"},
            {"timezone": "America/Chicago"},
        ),
        # Both NULL → no-op.
        (None, None, None),
    ],
)
@pytest.mark.asyncio
async def test_upsert_trading_hours_coalesce_matrix(
    session: AsyncSession,
    existing_hours: dict[str, str] | None,
    incoming_hours: dict[str, str] | None,
    expected_hours: dict[str, str] | None,
) -> None:
    """Pin the 4-cell COALESCE matrix on ``trading_hours`` so an idempotent
    re-upsert from a writer without IB contract details (``incoming=NULL``)
    can never clobber a prior populated row.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415

    sm = SecurityMaster(qualifier=AsyncMock(), db=session)

    # Seed first via upsert with existing_hours.
    await sm._upsert_definition_and_alias(
        raw_symbol="ZZZZ",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        alias_string="ZZZZ.NASDAQ",
        trading_hours=existing_hours,
    )
    await session.commit()

    # Re-upsert with incoming_hours.
    await sm._upsert_definition_and_alias(
        raw_symbol="ZZZZ",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        alias_string="ZZZZ.NASDAQ",
        trading_hours=incoming_hours,
    )
    await session.commit()

    row = (
        await session.execute(
            _select(InstrumentDefinition).where(
                InstrumentDefinition.raw_symbol == "ZZZZ",
                InstrumentDefinition.provider == "interactive_brokers",
                InstrumentDefinition.asset_class == "equity",
            )
        )
    ).scalar_one()
    assert row.trading_hours == expected_hours
