"""Full-lifecycle integration test for :class:`InstrumentRegistry`.

Walks a single ES futures definition through the complete lifecycle a live
trading deployment exercises end-to-end:

1. Create an :class:`InstrumentDefinition` for ES under
   ``provider='interactive_brokers'``.
2. Add an :class:`InstrumentAlias` for ``ESM6.CME`` with
   ``effective_from=date(2026, 3, 17)``.
3. Look up by alias with ``as_of_date=date(2026, 3, 17)`` — must return the
   definition.
4. Roll the contract: expire the ``ESM6.CME`` alias at ``date(2026, 6, 20)``
   and add a new ``ESU6.CME`` alias starting that same day. Then exercise
   the as-of windowing across the rollover boundary:

   - ``ESM6.CME`` @ ``date(2026, 5, 1)`` — still active, returns definition.
   - ``ESM6.CME`` @ ``date(2026, 9, 1)`` — expired, returns ``None``.
   - ``ESU6.CME`` @ ``date(2026, 9, 1)`` — active, returns definition.

Follows the per-module ``session_factory`` + ``isolated_postgres_url``
fixture pattern from ``test_instrument_registry.py`` /
``test_security_master_resolve_live.py`` — no shared fixture exists.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.registry import InstrumentRegistry

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
async def test_full_lifecycle_create_alias_roll_and_as_of_windowing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end lifecycle: create → alias → query → roll → query across windows."""
    async with session_factory() as session:
        # ------------------------------------------------------------------
        # Arrange / Act 1 — Create the ES definition
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Arrange / Act 2 — Add the June contract alias (ESM6.CME)
        # ------------------------------------------------------------------
        session.add(
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="ESM6.CME",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 3, 17),
            )
        )
        await session.commit()

        registry = InstrumentRegistry(session)

        # ------------------------------------------------------------------
        # Assert 3 — Same-day lookup of the newly-effective alias
        # ------------------------------------------------------------------
        result_same_day = await registry.find_by_alias(
            "ESM6.CME",
            provider="interactive_brokers",
            as_of_date=date(2026, 3, 17),
        )
        assert result_same_day is not None
        assert result_same_day.instrument_uid == idef.instrument_uid
        assert result_same_day.raw_symbol == "ES"

        # ------------------------------------------------------------------
        # Act 4 — Roll: expire ESM6.CME on 2026-06-20 and add ESU6.CME
        # ------------------------------------------------------------------
        m6_alias = next(
            a for a in idef.aliases if a.alias_string == "ESM6.CME"
        )
        m6_alias.effective_to = date(2026, 6, 20)
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

        # ------------------------------------------------------------------
        # Assert 5a — ESM6.CME @ 2026-05-01 (still active pre-roll)
        # ------------------------------------------------------------------
        result_pre_roll = await registry.find_by_alias(
            "ESM6.CME",
            provider="interactive_brokers",
            as_of_date=date(2026, 5, 1),
        )
        assert result_pre_roll is not None
        assert result_pre_roll.instrument_uid == idef.instrument_uid

        # ------------------------------------------------------------------
        # Assert 5b — ESM6.CME @ 2026-09-01 (expired post-roll)
        # ------------------------------------------------------------------
        result_post_roll_old = await registry.find_by_alias(
            "ESM6.CME",
            provider="interactive_brokers",
            as_of_date=date(2026, 9, 1),
        )
        assert result_post_roll_old is None

        # ------------------------------------------------------------------
        # Assert 5c — ESU6.CME @ 2026-09-01 (new contract active)
        # ------------------------------------------------------------------
        result_post_roll_new = await registry.find_by_alias(
            "ESU6.CME",
            provider="interactive_brokers",
            as_of_date=date(2026, 9, 1),
        )
        assert result_post_roll_new is not None
        assert result_post_roll_new.instrument_uid == idef.instrument_uid
        assert result_post_roll_new.raw_symbol == "ES"
