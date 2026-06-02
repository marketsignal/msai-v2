"""Integration tests for US-009 venue divergence counter."""

from __future__ import annotations

import pytest

from msai.services.nautilus.security_master.service import SecurityMaster
from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import REGISTRY_VENUE_DIVERGENCE_TOTAL

pytest_plugins = ["tests.integration.conftest_databento"]


def _counter_value(databento_venue: str, ib_venue: str) -> float:
    """Read the live divergence counter for a label pair DIRECTLY off the
    bound :data:`REGISTRY_VENUE_DIVERGENCE_TOTAL` object the production code
    increments — NOT via ``get_registry().render()``.

    Test-isolation fix (full-suite ordering): ``REGISTRY_VENUE_DIVERGENCE_TOTAL``
    is bound at ``trading_metrics`` import time and registered in the global
    ``MetricsRegistry`` ``_metrics`` dict. The autouse ``_reset_registry``
    fixture in ``tests/unit/test_metrics_endpoint.py`` calls
    ``get_registry().reset()``, which CLEARS that dict — orphaning the bound
    counter object from the registry's ``render()`` view while the object
    itself (which ``service.py`` increments) lives on. So once that fixture has
    run earlier in the same session, ``get_registry().render()`` no longer
    contains the divergence series and the old ``_extract_counter(render())``
    read saw ``0.0`` even though the increment fired. Reading the counter's own
    ``_values`` dict observes the SAME object the production code mutates, so it
    is correct regardless of registry-dict resets. DELTA-based (capture
    before/after for this label pair) so a residual cross-test increment on the
    venue-pair-only series is tolerated."""
    key = REGISTRY_VENUE_DIVERGENCE_TOTAL._key(
        {"databento_venue": databento_venue, "ib_venue": ib_venue}
    )
    with REGISTRY_VENUE_DIVERGENCE_TOTAL._lock:
        return REGISTRY_VENUE_DIVERGENCE_TOTAL._values.get(key, 0.0)


@pytest.mark.asyncio
async def test_divergence_counter_fires_on_mismatch(session_factory):
    """Seed Databento SPY.XARC → normalized to SPY.ARCA. Later IB refresh
    claims SPY.BATS. The divergence counter for labels
    (databento_venue=ARCA, ib_venue=BATS) increments by exactly one.

    DELTA-based (not an absolute ``... 1.0`` string match): the
    ``msai_registry_venue_divergence_total`` counter lives on a PROCESS-GLOBAL
    Prometheus registry that is NOT reset between tests, and its labels are
    venue-pair-only (NOT symbol-scoped). ``test_divergence_counter_does_not_re_
    fire_on_idempotent_ib_refresh`` also increments the SAME ``(ARCA, BATS)``
    series, so under a full-suite random order the absolute ``1.0`` assertion
    saw ``2.0`` and failed (both tests pass in isolation). Capture the series
    value BEFORE this test's IB refresh and assert it grew by exactly one,
    mirroring the existing delta style of
    ``test_divergence_counter_silent_on_match``."""
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

    before = _counter_value("ARCA", "BATS")

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

    after = _counter_value("ARCA", "BATS")
    assert after == before + 1.0, (
        f"the venue-divergence counter (databento=ARCA, ib=BATS) must increment "
        f"by exactly one on this mismatch (before={before}, after={after})"
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

    first_count = _counter_value("ARCA", "BATS")
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

    second_count = _counter_value("ARCA", "BATS")
    assert second_count == first_count, (
        f"idempotent refresh re-fired the counter ({first_count} → {second_count})"
    )
