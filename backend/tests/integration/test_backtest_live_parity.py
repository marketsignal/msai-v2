"""Backtest <-> live parity integration test (PRD US-001).

Pins ``datetime.now()`` via freezegun so both resolve paths observe the same
``as_of_date`` and therefore hit the same active aliases, verifying the
invariant that a strategy referencing ``["AAPL", "ES"]`` sees the identical
canonical Nautilus ``InstrumentId`` strings in backtest and live execution.

Scope: AAPL + ES are warm-path resolves — the test only exercises the
registry lookup path, not the ``.Z.N`` Databento continuous synthesis (that
parity is covered by the end-to-end continuous-futures backtest test).

Follows the per-module ``session_factory`` fixture pattern shared across the
registry integration tests. ``mock_qualifier`` is constructed inline inside
the test body (not a shared fixture) — it is never invoked on this warm-hit
path, but both resolve entrypoints require a non-``None`` ``SecurityMaster``
to be constructed successfully.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
from msai.services.nautilus.security_master.live_resolver import lookup_for_live
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
@freeze_time("2026-04-17")
async def test_resolve_live_and_backtest_return_identical_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PRD US-001: same strategy sees same InstrumentId strings across
    backtest and live paths. Both resolve ["AAPL", "ES"] to
    ["AAPL.NASDAQ", "ESM6.CME"].

    AAPL + ES warm-path resolves hit the registry only; no Databento
    continuous synthesis path is exercised here (ES is not ``.Z.N``)."""
    async with session_factory() as session:
        for provider in ("interactive_brokers", "databento"):
            session.add(
                InstrumentDefinition(
                    raw_symbol="AAPL",
                    listing_venue="NASDAQ",
                    routing_venue="NASDAQ",
                    asset_class="equity",
                    provider=provider,
                    lifecycle_state="active",
                )
            )
            session.add(
                InstrumentDefinition(
                    raw_symbol="ES",
                    listing_venue="CME",
                    routing_venue="CME",
                    asset_class="futures",
                    provider=provider,
                    lifecycle_state="active",
                )
            )
        await session.flush()
        for row in (await session.execute(select(InstrumentDefinition))).scalars():
            alias_string = "AAPL.NASDAQ" if row.raw_symbol == "AAPL" else "ESM6.CME"
            session.add(
                InstrumentAlias(
                    instrument_uid=row.instrument_uid,
                    alias_string=alias_string,
                    venue_format="exchange_name",
                    provider=row.provider,
                    effective_from=date(2026, 3, 17),
                )
            )
        await session.commit()
        # Expire all cached attributes so the next SELECT re-fires ``selectin``
        # against the now-committed aliases. Without this, the definition
        # instances carry the pre-alias (empty) collection from the SELECT
        # above, and ``resolve_for_backtest`` sees idef.aliases == [] from
        # identity-map caching — not a DB state issue, a session state issue.
        session.expire_all()

        mock_qualifier = MagicMock()
        mock_qualifier.qualify = AsyncMock()
        sm = SecurityMaster(qualifier=mock_qualifier, db=session)
        backtest_ids = await sm.resolve_for_backtest(["AAPL", "ES"])
        resolved = await lookup_for_live(
            ["AAPL", "ES"], as_of_date=exchange_local_today(), session=session
        )
        live_ids = [r.canonical_id for r in resolved]

    assert live_ids == backtest_ids, (
        f"PRD US-001 parity violation: live={live_ids!r} vs backtest={backtest_ids!r}"
    )
    assert live_ids == ["AAPL.NASDAQ", "ESM6.CME"]
