"""Advisory-lock race + source_venue_raw provenance tests for Databento
bootstrap write path. Uses testcontainers session_factory fixture from
conftest_databento.py (T0)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from msai.models.instrument_alias import InstrumentAlias
from msai.services.nautilus.security_master.service import SecurityMaster

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_concurrent_databento_upserts_serialize_via_advisory_lock(session_factory):
    """Two concurrent alias-rotation calls for the same
    (raw_symbol, provider, asset_class) leave exactly ONE active alias.
    Without the advisory lock they can leave two rows with effective_to
    IS NULL."""

    async def _rotate_to(alias_mic: str) -> None:
        async with session_factory() as session:
            sm = SecurityMaster(db=session, databento_client=None)
            await sm._upsert_definition_and_alias(
                raw_symbol="SPY",
                listing_venue="ARCA",
                routing_venue="SMART",
                asset_class="equity",
                alias_string=f"SPY.{alias_mic}",
                provider="databento",
                venue_format="mic_code",
            )
            await session.commit()

    # Seed
    await _rotate_to("XARC")
    # Concurrent rotations to different aliases. With the relaxed CHECK
    # (effective_to >= effective_from, migration b6c7d8e9f0a1) same-day
    # rotations no longer trip the constraint; the invariant under test
    # is still that the final state has exactly one active alias — the
    # advisory lock serializes the two racing UPSERTs so we don't leave
    # two rows with effective_to IS NULL.
    await asyncio.gather(
        _rotate_to("BATS"),
        _rotate_to("EDGX"),
        return_exceptions=True,
    )

    async with session_factory() as session:
        result = await session.execute(
            select(InstrumentAlias)
            .where(InstrumentAlias.provider == "databento")
            .where(InstrumentAlias.effective_to.is_(None))
        )
        active = result.scalars().all()
    assert len(active) == 1, (
        f"expected 1 active alias after concurrent rotations, got {len(active)}: "
        f"{[a.alias_string for a in active]}"
    )


@pytest.mark.asyncio
async def test_same_day_alias_round_trip_keeps_one_active(session_factory):
    """Same-day sequence A → B → A on the same ``(raw_symbol, provider)``
    must end with exactly ONE active alias (the latest, A).

    Regression test: previously the third call closed B (because
    ``alias_string != A`` and ``effective_to IS NULL``), but the A insert
    was a no-op due to ON CONFLICT DO NOTHING on
    ``(alias_string, provider, effective_from)`` — leaving ZERO active
    aliases. Switching the alias UPSERT to ON CONFLICT DO UPDATE SET
    ``effective_to = NULL`` revives the existing same-day row.
    """

    async def _rotate_to(alias_string: str) -> None:
        async with session_factory() as session:
            sm = SecurityMaster(db=session, databento_client=None)
            await sm._upsert_definition_and_alias(
                raw_symbol="MSFT",
                listing_venue="NASDAQ",
                routing_venue="SMART",
                asset_class="equity",
                alias_string=alias_string,
                provider="databento",
                venue_format="exchange_name",
            )
            await session.commit()

    await _rotate_to("MSFT.NASDAQ")
    await _rotate_to("MSFT.NYSE")
    await _rotate_to("MSFT.NASDAQ")  # the trap case

    async with session_factory() as session:
        active = (
            (
                await session.execute(
                    select(InstrumentAlias)
                    .where(InstrumentAlias.provider == "databento")
                    .where(InstrumentAlias.alias_string.like("MSFT.%"))
                    .where(InstrumentAlias.effective_to.is_(None))
                )
            )
            .scalars()
            .all()
        )

    assert len(active) == 1, (
        f"A → B → A round-trip must leave exactly 1 active alias; got "
        f"{[a.alias_string for a in active]}"
    )
    assert active[0].alias_string == "MSFT.NASDAQ", (
        f"the latest alias (NASDAQ) must be the active one; got {active[0].alias_string}"
    )


@pytest.mark.asyncio
async def test_source_venue_raw_populated_on_databento_write(session_factory):
    """provider='databento' writes preserve the raw MIC in source_venue_raw
    even after alias_string is normalized to exchange-name."""
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="AAPL.XNAS",  # pre-normalization
            provider="databento",
            venue_format="mic_code",
        )
        await session.commit()

    async with session_factory() as session:
        row = (
            await session.execute(
                select(InstrumentAlias).where(InstrumentAlias.alias_string == "AAPL.NASDAQ")
            )
        ).scalar_one()
    assert row.source_venue_raw == "XNAS"
