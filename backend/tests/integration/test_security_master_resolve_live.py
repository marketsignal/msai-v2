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
                    select(InstrumentDefinition).where(InstrumentDefinition.raw_symbol == "MSFT")
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


@pytest.mark.asyncio
async def test_resolve_for_live_es_routes_through_fixed_month_future(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression (plan-review iter 1 P1, iter 2/3/4 refined): resolving
    ES through ``resolve_for_live``'s cold path must build a FUT spec
    with expiry — NOT a CONTFUT spec (expiry=None), and NOT an
    ESM6-rooted spec that would double-encode the month via
    ``InstrumentSpec.canonical_id`` (producing "ESM6M6.CME").

    Asserts:
      (a) expiry set on the spec (rules out CONTFUT)
      (b) root symbol 'ES' (not 'ESM6' — rules out duplicate-month bug)
      (c) asset_class + venue are future/CME (full roundtrip shape).

    Uses the same mock shape as
    ``test_resolve_for_live_cold_miss_calls_ib_and_upserts``.
    """
    async with session_factory() as session:
        captured_specs: list = []

        fake_instrument = MagicMock()
        fake_instrument.id = MagicMock()
        fake_instrument.id.__str__ = MagicMock(return_value="ESM6.CME")
        fake_instrument.id.venue.value = "CME"
        fake_instrument.raw_symbol.value = "ES"
        fake_instrument.__class__.__name__ = "FuturesContract"
        fake_instrument.to_dict = MagicMock(
            return_value={
                "type": "FuturesContract",
                "instrument_id": "ESM6.CME",
                "raw_symbol": "ES",
            }
        )

        async def _capture_and_return(spec):
            captured_specs.append(spec)
            return fake_instrument

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock(side_effect=_capture_and_return)

        mock_provider = MagicMock()
        fake_details = MagicMock()
        fake_details.contract.primaryExchange = "CME"
        mock_provider.contract_details = {fake_instrument.id: fake_details}
        mock_qualifier._provider = mock_provider

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)

        # Act
        ids = await sm.resolve_for_live(["ES"])

        # Assert — return value shape
        assert len(ids) == 1

        # Assert — the spec the qualifier got
        assert len(captured_specs) == 1
        spec = captured_specs[0]
        assert spec.asset_class == "future"
        assert spec.venue == "CME"
        assert spec.expiry is not None, "gotcha: no expiry → maps to CONTFUT"
        assert spec.symbol == "ES", (
            f"must be root 'ES', not local-symbol 'ESM6' — otherwise "
            f"InstrumentSpec.canonical_id produces 'ESM6M6.CME'. "
            f"Got: {spec.symbol!r}"
        )


@pytest.mark.asyncio
async def test_resolve_for_live_warm_raw_symbol_falls_through_on_stale_alias(
    session_factory: async_sessionmaker[async_sessionmaker[AsyncSession] | AsyncSession],  # type: ignore[type-arg]
) -> None:
    """Regression (iter-7 P1): if the registry has an active ES alias
    pointing at an old front month (e.g. ``ESM6.CME`` after the June
    expiry), a bare-ticker refresh (``msai instruments refresh
    --symbols ES``) must NOT return that stale alias. Instead the
    warm-B path recomputes today's canonical, sees it differs, and
    falls through to the cold path to re-qualify + close-and-open.
    """
    async with session_factory() as session:
        # Arrange: seed the registry with a STALE active ES alias.
        # We store ``ESH1.CME`` (March 2021) which is guaranteed to
        # differ from whatever today's front month is.
        idef = InstrumentDefinition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="interactive_brokers",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESH1.CME",  # deliberately stale
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2021, 1, 1),
            )
        )
        await session.commit()

        # Stub qualifier: if warm path B returned the stale alias,
        # the cold path wouldn't fire and qualify wouldn't be called.
        # If the cold path DID fire, qualify IS called — which is
        # what we assert below.
        from unittest.mock import AsyncMock, MagicMock

        fake_instrument = MagicMock()
        fake_instrument.id = MagicMock()
        today_canonical = None
        try:
            from msai.services.nautilus.live_instrument_bootstrap import (
                canonical_instrument_id,
            )

            today_canonical = canonical_instrument_id("ES")
        except Exception:  # pragma: no cover — sanity
            today_canonical = "ESM6.CME"
        fake_instrument.id.__str__ = MagicMock(return_value=today_canonical)
        fake_instrument.id.venue.value = "CME"
        fake_instrument.raw_symbol.value = "ES"
        fake_instrument.__class__.__name__ = "FuturesContract"
        fake_instrument.to_dict = MagicMock(
            return_value={
                "type": "FuturesContract",
                "instrument_id": today_canonical,
                "raw_symbol": "ES",
            }
        )

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock(return_value=fake_instrument)

        mock_provider = MagicMock()
        fake_details = MagicMock()
        fake_details.contract.primaryExchange = "CME"
        mock_provider.contract_details = {fake_instrument.id: fake_details}
        mock_qualifier._provider = mock_provider

        sm = SecurityMaster(qualifier=mock_qualifier, db=session)
        resolved = await sm.resolve_for_live(["ES"])

        # Cold path MUST have fired — qualify was called.
        mock_qualifier.qualify.assert_awaited()
        # And the result must be today's canonical (not the stale stored alias).
        assert resolved == [today_canonical]


@pytest.mark.asyncio
async def test_resolve_for_live_warm_honors_nonrollable_alias_move(
    session_factory: async_sessionmaker[async_sessionmaker[AsyncSession] | AsyncSession],  # type: ignore[type-arg]
) -> None:
    """Regression (iter-8 P1): for non-rollable symbols (AAPL/MSFT/
    SPY/EUR/USD), the registry's active alias is authoritative — a
    legitimate alias move (e.g. AAPL.NASDAQ → AAPL.ARCA) must be
    returned as-is by warm path B, NOT reverted to
    canonical_instrument_id's hardcoded default.

    Otherwise warm-only callers (qualifier=None) would raise
    'Cold-miss resolve ... requires an IBQualifier' after any venue
    change that IB qualification returned.
    """
    async with session_factory() as session:
        # Arrange: AAPL was moved to ARCA (hypothetical but plausible).
        idef = InstrumentDefinition(
            raw_symbol="AAPL",
            listing_venue="ARCA",
            routing_venue="ARCA",
            asset_class="equity",
            provider="interactive_brokers",
            lifecycle_state="active",
        )
        session.add(idef)
        await session.flush()
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.ARCA",  # moved from NASDAQ
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

        # qualifier=None — warm-only caller. Must NOT raise.
        sm = SecurityMaster(qualifier=None, db=session)
        resolved = await sm.resolve_for_live(["AAPL"])

        assert resolved == ["AAPL.ARCA"], (
            f"warm path B should honor the registry's active alias "
            f"(AAPL.ARCA) for non-rollable AAPL; got {resolved!r}"
        )


# ---------------------------------------------------------------------------
# Task 3b — registry signature locks + structured AmbiguousSymbolError
# ---------------------------------------------------------------------------


def test_find_by_alias_requires_as_of_date() -> None:
    """Regression lock: iter-2 plan-review P1 — UTC default regresses
    roll-day correctness if any caller forgets to pass it.

    :meth:`InstrumentRegistry.find_by_alias`'s ``as_of_date`` kwarg must
    be required, not defaulted. Callers MUST thread an explicit
    exchange-local date (Chicago-local ``spawn_today`` / CME trading
    date) so that a late-UTC-night run doesn't silently resolve to a
    different quarterly futures contract than the rest of the
    live-path wiring computed.
    """
    import inspect

    from msai.services.nautilus.security_master.registry import InstrumentRegistry

    sig = inspect.signature(InstrumentRegistry.find_by_alias)
    param = sig.parameters["as_of_date"]
    assert param.default is inspect.Parameter.empty, (
        "as_of_date must be required — UTC default regresses roll-day behavior"
    )


def test_require_definition_requires_as_of_date() -> None:
    """Same rationale as ``test_find_by_alias_requires_as_of_date`` for
    the thin wrapper :meth:`InstrumentRegistry.require_definition` —
    leaving a default on the wrapper would reintroduce the silent
    UTC-vs-exchange-date skew via the convenience path.
    """
    import inspect

    from msai.services.nautilus.security_master.registry import InstrumentRegistry

    sig = inspect.signature(InstrumentRegistry.require_definition)
    param = sig.parameters["as_of_date"]
    assert param.default is inspect.Parameter.empty, (
        "as_of_date must be required on require_definition too"
    )


def test_ambiguous_symbol_error_exposes_structured_attributes() -> None:
    """Task 3's ``lookup_for_live`` wraps this error; must read
    ``asset_classes`` as a list attribute, not via string parsing of
    the formatted message.

    The new ``AmbiguousSymbolError.__init__`` takes keyword args
    ``symbol`` / ``provider`` / ``asset_classes`` and composes the
    human-readable message itself — so callers can route on the
    attributes deterministically while ``pytest.raises(..., match="SPY")``
    continues to work because the symbol is embedded in the message.
    """
    from msai.services.nautilus.security_master.registry import (
        AmbiguousSymbolError,
    )

    err = AmbiguousSymbolError(
        symbol="SPY",
        provider="interactive_brokers",
        asset_classes=["equity", "option"],
    )
    assert err.symbol == "SPY"
    assert err.provider == "interactive_brokers"
    assert err.asset_classes == ["equity", "option"]
    # Message still contains "SPY" so existing `match="SPY"` tests keep passing.
    assert "SPY" in str(err)
