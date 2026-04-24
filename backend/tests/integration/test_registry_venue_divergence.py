"""Integration tests for US-009 venue divergence counter."""

from __future__ import annotations

import pytest

from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.observability import get_registry

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_divergence_counter_fires_on_mismatch(session_factory):
    """Seed Databento SPY.XARC → normalized to SPY.ARCA. Later IB refresh
    claims SPY.BATS. Counter increments with labels
    (databento_venue=ARCA, ib_venue=BATS)."""
    # Seed
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY",
            listing_venue="ARCA",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="SPY.XARC",
            provider="databento",
            venue_format="mic_code",
        )
        await session.commit()

    # IB refresh with a different venue (hypothetical migration)
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="SPY",
            listing_venue="BATS",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="SPY.BATS",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    rendered = get_registry().render()
    # Labels alphabetical per metrics.py:61 _format_labels(); value is float.
    assert (
        'msai_registry_venue_divergence_total{databento_venue="ARCA",ib_venue="BATS"} 1.0'
        in rendered
    )


@pytest.mark.asyncio
async def test_divergence_counter_silent_on_match(session_factory):
    """Seed Databento SPY.XARC → ARCA. IB refresh with SPY.ARCA — venues
    match, counter does NOT fire."""
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="QQQ",  # different symbol so test isolation holds
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="QQQ.XNAS",
            provider="databento",
            venue_format="mic_code",
        )
        await session.commit()

    before = get_registry().render()

    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="QQQ",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="QQQ.NASDAQ",  # already exchange-name, same venue
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    after = get_registry().render()
    # Count lines should be unchanged (no new QQQ-labeled increment)
    before_qqq = sum(
        1
        for line in before.splitlines()
        if 'databento_venue="NASDAQ"' in line and 'ib_venue="NASDAQ"' in line
    )
    after_qqq = sum(
        1
        for line in after.splitlines()
        if 'databento_venue="NASDAQ"' in line and 'ib_venue="NASDAQ"' in line
    )
    assert before_qqq == after_qqq  # unchanged — no increment on match


@pytest.mark.asyncio
async def test_divergence_counter_does_not_re_fire_on_idempotent_ib_refresh(session_factory):
    """Regression for Codex P2 (2026-04-24) — after a real migration
    (Databento=ARCA, IB=BATS), an idempotent IB re-refresh with the
    SAME BATS venue must NOT re-increment the divergence counter.
    The gate is ``new_ib_venue != prior_ib_venue`` AND
    ``new_ib_venue != prior_databento_venue``."""
    # Seed Databento IWM.ARCA.
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="IWM",
            listing_venue="ARCA",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="IWM.XARC",
            provider="databento",
            venue_format="mic_code",
        )
        await session.commit()

    # First IB refresh — real migration ARCA→BATS. Counter should fire.
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="IWM",
            listing_venue="BATS",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="IWM.BATS",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    rendered_after_first = get_registry().render()
    first_count = _extract_counter(rendered_after_first, "ARCA", "BATS")
    assert first_count >= 1.0, "first real migration must fire the counter"

    # Second IB refresh — same BATS venue, IB alias didn't transition.
    # Counter must NOT re-fire.
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="IWM",
            listing_venue="BATS",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="IWM.BATS",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    rendered_after_second = get_registry().render()
    second_count = _extract_counter(rendered_after_second, "ARCA", "BATS")
    assert second_count == first_count, (
        f"idempotent refresh re-fired the counter ({first_count} → {second_count})"
    )


def _extract_counter(rendered: str, databento_venue: str, ib_venue: str) -> float:
    """Pull the current value of the divergence counter for a given label pair.

    Returns 0.0 if no matching counter line exists yet.
    """
    target = (
        f'msai_registry_venue_divergence_total{{databento_venue="{databento_venue}",'
        f'ib_venue="{ib_venue}"}}'
    )
    for line in rendered.splitlines():
        if line.startswith(target):
            return float(line.rsplit(" ", 1)[1])
    return 0.0
