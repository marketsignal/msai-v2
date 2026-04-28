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
from uuid import uuid4

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


async def _seed_uid_for_alias_tests(
    session: AsyncSession,
    raw: str,
    alias_str: str,
    eff_to: date | None,
) -> None:
    """Seed one InstrumentDefinition + one InstrumentAlias keyed by uuid4().

    Lives at module scope so the new ``find_by_aliases_bulk`` tests below
    have a single seed helper. Inlines the schema columns the registry
    selects on (``provider``, ``asset_class``, ``effective_from``,
    ``effective_to``).
    """
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
            alias_string=alias_str,
            venue_format="exchange_name",
            provider="interactive_brokers",
            effective_from=date(2026, 1, 1),
            effective_to=eff_to,
        )
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


@pytest.mark.asyncio
async def test_find_by_aliases_bulk_returns_dict_of_active_aliases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """find_by_aliases_bulk maps every active alias to its definition.
    Misses are absent from the dict so callers can use ``in`` for membership.

    Uses the per-module ``session_factory`` fixture pattern (the only fixture
    defined in this test module — no shared ``session`` fixture exists).
    """
    today = date(2026, 4, 27)

    async with session_factory() as session:
        # Seed AAPL.NASDAQ + MSFT.NASDAQ (active); seed FOO.NASDAQ effective_to in past
        await _seed_uid_for_alias_tests(session, "AAPL", "AAPL.NASDAQ", None)
        await _seed_uid_for_alias_tests(session, "MSFT", "MSFT.NASDAQ", None)
        await _seed_uid_for_alias_tests(session, "FOO", "FOO.NASDAQ", date(2026, 1, 1))
        await session.commit()

        registry = InstrumentRegistry(session)
        result = await registry.find_by_aliases_bulk(
            ["AAPL.NASDAQ", "MSFT.NASDAQ", "FOO.NASDAQ", "MISS.NASDAQ"],
            provider="interactive_brokers",
            as_of_date=today,
        )

    # Assert
    assert set(result.keys()) == {"AAPL.NASDAQ", "MSFT.NASDAQ"}, (
        "FOO.NASDAQ has effective_to in the past — should be absent. "
        "MISS.NASDAQ has no row — should be absent."
    )
    assert result["AAPL.NASDAQ"].raw_symbol == "AAPL"
    assert result["MSFT.NASDAQ"].raw_symbol == "MSFT"


@pytest.mark.asyncio
async def test_find_by_aliases_bulk_empty_input_returns_empty_dict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        registry = InstrumentRegistry(session)
        result = await registry.find_by_aliases_bulk(
            [], provider="interactive_brokers", as_of_date=date.today()
        )
    assert result == {}


# ---------------------------------------------------------------------------
# Boundary semantics — alias windowing is half-open ``[from, to)``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "expect_present"),
    [
        # ``effective_to=date(2026, 4, 27)`` excludes the 2026-04-27 query —
        # the half-open window is ``[from, to)``.
        (date(2026, 4, 27), False),
        # The day before the close-out is still inside the window.
        (date(2026, 4, 26), True),
    ],
)
@pytest.mark.asyncio
async def test_find_by_aliases_bulk_window_boundary_is_half_open(
    session_factory: async_sessionmaker[AsyncSession],
    as_of: date,
    expect_present: bool,
) -> None:
    """``effective_to`` is the EXCLUSIVE end of the alias window
    (``effective_from <= as_of < effective_to``). A query at
    ``as_of=effective_to`` MUST NOT include the row — that's how the
    futures-roll close-out boundary stays unambiguous.
    """
    async with session_factory() as session:
        await _seed_uid_for_alias_tests(session, "BNDY", "BNDY.NASDAQ", date(2026, 4, 27))
        await session.commit()

        registry = InstrumentRegistry(session)
        result = await registry.find_by_aliases_bulk(
            ["BNDY.NASDAQ"],
            provider="interactive_brokers",
            as_of_date=as_of,
        )

    if expect_present:
        assert "BNDY.NASDAQ" in result
    else:
        assert "BNDY.NASDAQ" not in result


# ---------------------------------------------------------------------------
# Cross-provider isolation — bulk lookup honors the provider namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_aliases_bulk_cross_provider_isolation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Schema uniqueness is ``(alias_string, provider, effective_from)`` — the
    same alias_string CAN coexist under both providers. ``find_by_aliases_bulk``
    MUST filter by provider so a Databento alias never bleeds into the
    interactive_brokers warm-hit set (and vice versa)."""
    today = date(2026, 4, 27)

    async with session_factory() as session:
        # Seed the same alias_string under BOTH providers — distinct
        # InstrumentDefinition rows because (raw_symbol, provider, asset_class)
        # is unique per provider.
        ib_uid = uuid4()
        db_uid = uuid4()
        for uid, provider in ((ib_uid, "interactive_brokers"), (db_uid, "databento")):
            session.add(
                InstrumentDefinition(
                    instrument_uid=uid,
                    raw_symbol="DUAL",
                    provider=provider,
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
                    alias_string="DUAL.NASDAQ",
                    venue_format="exchange_name",
                    provider=provider,
                    effective_from=date(2026, 1, 1),
                    effective_to=None,
                )
            )
        await session.commit()

        registry = InstrumentRegistry(session)
        ib_only = await registry.find_by_aliases_bulk(
            ["DUAL.NASDAQ"],
            provider="interactive_brokers",
            as_of_date=today,
        )

    assert "DUAL.NASDAQ" in ib_only
    # Critical: the row returned is the IB-side definition, not the Databento one.
    assert ib_only["DUAL.NASDAQ"].provider == "interactive_brokers"
    assert ib_only["DUAL.NASDAQ"].instrument_uid == ib_uid
