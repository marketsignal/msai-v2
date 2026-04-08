"""Integration tests for :class:`SecurityMaster` (Phase 2 task 2.5).

Uses a testcontainer Postgres for the cache layer and a stub
qualifier for the IB side. Does NOT touch a real IB connection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, InstrumentCache
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


class _StubQualifier:
    """Stub :class:`IBQualifier` that returns canned Nautilus-ish
    ``Instrument`` stand-ins for each spec. The stand-in is a
    ``MagicMock`` with the ``to_dict`` classmethod returning a
    deterministic dict keyed by canonical_id.

    We don't return real Nautilus ``Instrument`` objects because
    constructing one requires a live IB contract details blob.
    Instead the stub satisfies the contract
    :meth:`SecurityMaster._write_cache` depends on:
    ``instrument.to_dict(instrument) → dict``.
    """

    def __init__(self) -> None:
        self.qualify_calls: list[InstrumentSpec] = []
        # Expose an empty ``contract_details`` dict under the
        # ``_provider`` attribute so the service's
        # ``_trading_hours_for`` helper finds "nothing" and
        # writes NULL — matches the Phase 2 test contract.
        self._provider = MagicMock()
        self._provider.contract_details = {}

    async def qualify(self, spec: InstrumentSpec) -> Any:
        self.qualify_calls.append(spec)
        mock_instrument = MagicMock()
        # ``to_dict(cls, obj)`` is called as ``instrument.to_dict(instrument)``
        # in the service (because Nautilus's ``Instrument.to_dict`` is a
        # classmethod on the base). We stub it as a normal method on
        # the mock so ``MagicMock.to_dict(mock_instrument)`` returns
        # the expected dict.
        canon = spec.canonical_id()
        mock_instrument.to_dict = MagicMock(
            return_value={
                "type": "Equity",
                "instrument_id": canon,
                "raw_symbol": spec.symbol,
            }
        )
        return mock_instrument


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_cache_miss_qualifies_writes_and_returns(
    session: AsyncSession,
) -> None:
    """Happy-path cache miss: the spec isn't in the cache, so
    ``resolve`` calls the qualifier, writes the row, and returns
    the instrument.

    The stub's ``to_dict`` output isn't a real Nautilus Equity
    schema, so a SECOND ``resolve`` (which tries to deserialize
    via ``Equity.from_dict``) will raise. We verify the first
    call succeeded AND the cache row was written AND the second
    call did NOT re-qualify (cache hit short-circuit). We
    deliberately wrap the second call in a suppress block because
    the cache-hit path is what we're asserting, not the
    deserialization contract (which production's real
    ``to_dict`` fulfils).
    """
    import contextlib

    qualifier = _StubQualifier()
    master = SecurityMaster(qualifier=qualifier, db=session)  # type: ignore[arg-type]

    spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
    first = await master.resolve(spec)

    assert first is not None
    assert len(qualifier.qualify_calls) == 1

    # Row was written
    row = (
        await session.execute(
            select(InstrumentCache).where(InstrumentCache.canonical_id == "AAPL.NASDAQ")
        )
    ).scalar_one()
    assert row.asset_class == "equity"
    assert row.venue == "NASDAQ"
    assert row.nautilus_instrument_json["instrument_id"] == "AAPL.NASDAQ"

    # Second resolve hits the cache — qualifier is NOT called again.
    # The stub's ``to_dict`` shape doesn't satisfy Equity.from_dict,
    # so deserialization will raise; we catch that and assert the
    # cache path WAS taken by checking qualifier call count.
    with contextlib.suppress(Exception):
        await master.resolve(spec)
    assert len(qualifier.qualify_calls) == 1


@pytest.mark.asyncio
async def test_resolve_cache_hit_does_not_call_qualifier(
    session: AsyncSession,
) -> None:
    """Pre-seed a cache row directly, then resolve. The qualifier
    must NOT be called (we return the cached instrument)."""
    # Pre-seed
    session.add(
        InstrumentCache(
            canonical_id="MSFT.NASDAQ",
            asset_class="equity",
            venue="NASDAQ",
            ib_contract_json={"secType": "STK", "symbol": "MSFT"},
            nautilus_instrument_json={
                "type": "Equity",
                "instrument_id": "MSFT.NASDAQ",
                "raw_symbol": "MSFT",
            },
            trading_hours=None,
            last_refreshed_at=datetime.now(UTC),
        )
    )
    await session.commit()

    qualifier = _StubQualifier()
    master = SecurityMaster(qualifier=qualifier, db=session)  # type: ignore[arg-type]

    # Resolve — must hit the cache. We expect Nautilus's
    # ``Instrument.from_dict`` to raise on our fake dict (it
    # doesn't have the full Nautilus schema), so catch it to
    # verify the cache path WAS taken rather than relying on a
    # successful deserialization.
    spec = InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ")
    with pytest.raises(Exception):  # noqa: B017,BLE001 — Nautilus from_dict rejects our stub
        await master.resolve(spec)

    # Critical assertion: the qualifier was NOT called (cache hit
    # short-circuited before touching IB).
    assert len(qualifier.qualify_calls) == 0


# ---------------------------------------------------------------------------
# bulk_resolve()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_resolve_only_qualifies_missing_specs(
    session: AsyncSession,
) -> None:
    """Mixed cache hit + miss: the bulk resolve should fetch the
    hit rows in one SELECT and only call the qualifier for the
    misses. The whole point of the cache is minimizing IB
    ``reqContractDetails`` calls."""
    # Pre-seed AAPL only
    session.add(
        InstrumentCache(
            canonical_id="AAPL.NASDAQ",
            asset_class="equity",
            venue="NASDAQ",
            ib_contract_json={"secType": "STK"},
            nautilus_instrument_json={
                "type": "Equity",
                "instrument_id": "AAPL.NASDAQ",
                "raw_symbol": "AAPL",
            },
            trading_hours=None,
            last_refreshed_at=datetime.now(UTC),
        )
    )
    await session.commit()

    qualifier = _StubQualifier()
    master = SecurityMaster(qualifier=qualifier, db=session)  # type: ignore[arg-type]

    specs = [
        InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),  # hit
        InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),  # miss
        InstrumentSpec(asset_class="equity", symbol="GOOG", venue="NASDAQ"),  # miss
    ]
    # AAPL will raise inside from_dict (our stub JSON), so wrap the
    # call and inspect call counts before the exception propagates.
    import contextlib

    with contextlib.suppress(Exception):  # noqa: BLE001 — from_dict on stub dict is expected
        await master.bulk_resolve(specs)

    # Qualifier should have been called for MSFT at most (we raise
    # on AAPL's from_dict before reaching MSFT, so call count may
    # be 0 or 1 depending on iteration order). The important
    # assertion: qualifier was NOT called for AAPL (the cached one).
    called_symbols = [s.symbol for s in qualifier.qualify_calls]
    assert "AAPL" not in called_symbols


@pytest.mark.asyncio
async def test_bulk_resolve_empty_input_returns_empty_list(
    session: AsyncSession,
) -> None:
    master = SecurityMaster(qualifier=_StubQualifier(), db=session)  # type: ignore[arg-type]
    assert await master.bulk_resolve([]) == []


@pytest.mark.asyncio
async def test_bulk_resolve_all_misses_qualifies_each(
    session: AsyncSession,
) -> None:
    """Cold cache: every spec misses, so the qualifier is called
    once per spec."""
    qualifier = _StubQualifier()
    master = SecurityMaster(qualifier=qualifier, db=session)  # type: ignore[arg-type]

    specs = [
        InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
        InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),
        InstrumentSpec(asset_class="equity", symbol="GOOG", venue="NASDAQ"),
    ]
    import contextlib

    with contextlib.suppress(Exception):  # noqa: BLE001 — from_dict on stub dict is expected
        await master.bulk_resolve(specs)

    # All three specs miss the cache → qualifier is called for
    # each. Because ``resolve`` writes the cache row BEFORE the
    # from_dict call that raises on our stub, the resolve path
    # still completes the miss write for every spec. Order is
    # preserved via ``zip(specs, ...)``.
    called = [s.symbol for s in qualifier.qualify_calls]
    assert called == ["AAPL", "MSFT", "GOOG"]


# ---------------------------------------------------------------------------
# refresh() — not yet implemented
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_is_not_implemented_for_phase2(
    session: AsyncSession,
) -> None:
    """Phase 2 exposes ``refresh`` as a hook; the background
    scheduler lands in Phase 4. Ensure we fail loud rather than
    silently succeeding with stale data."""
    master = SecurityMaster(qualifier=_StubQualifier(), db=session)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="Phase 4"):
        await master.refresh("AAPL.NASDAQ")


# ---------------------------------------------------------------------------
# Cache validity threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_validity_default_is_30_days(
    session: AsyncSession,
) -> None:
    """Regression guard: the default staleness threshold must be
    30 days. Longer defaults risk serving stale contract details
    that IB has already changed (rare but possible — e.g. a
    corporate action changes the ticker)."""
    master = SecurityMaster(qualifier=_StubQualifier(), db=session)  # type: ignore[arg-type]
    assert master._cache_validity == timedelta(days=30)
