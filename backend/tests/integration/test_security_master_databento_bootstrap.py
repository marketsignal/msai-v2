"""End-to-end integration tests for DatabentoBootstrapService.

Covers idempotency (NOOP), rotation (ALIAS_ROTATED), and the
ambiguity→exact_id retry flow against a real testcontainers Postgres."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapOutcome,
    DatabentoBootstrapService,
)

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_same_symbol_twice_returns_noop(session_factory, mock_databento):
    """Second bootstrap of same symbol with same canonical ID → outcome=noop."""
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )

    first = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert first[0].outcome == BootstrapOutcome.CREATED

    second = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert second[0].outcome == BootstrapOutcome.NOOP
    assert second[0].registered is True


@pytest.mark.asyncio
async def test_changed_mic_returns_alias_rotated(session_factory, mock_databento):
    """Re-bootstrap with a different Databento venue on the same calendar
    day → alias_rotated. Regression guard for the same-day CHECK relaxation
    (migration b6c7d8e9f0a1): the closing UPDATE stamps
    ``effective_to=today`` on a row whose ``effective_from`` is also today,
    producing a zero-width ``[F, F)`` audit row. With the relaxed CHECK
    ``effective_to >= effective_from``, this path is now production-safe;
    previously it raised IntegrityError on the strict ``>``."""
    from tests.integration.conftest_databento import _make_equity_instrument

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )

    # Seed: mock returns SPY.XARC → normalizes to SPY.ARCA (today).
    await svc.bootstrap(symbols=["SPY"], asset_class_override=None, exact_ids=None)

    # Reconfigure mock to return SPY on a DIFFERENT venue (simulated migration).
    def _rotated_side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        if symbol == "SPY":
            return [_make_equity_instrument("SPY", "BATS")]
        raise RuntimeError(f"unexpected symbol {symbol}")

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_rotated_side_effect)

    # Rotate on the SAME calendar day — exercises the zero-width audit-row path.
    second = await svc.bootstrap(symbols=["SPY"], asset_class_override=None, exact_ids=None)
    assert second[0].outcome == BootstrapOutcome.ALIAS_ROTATED
    assert second[0].canonical_id == "SPY.BATS"


@pytest.mark.asyncio
async def test_live_qualified_true_when_ib_alias_exists(session_factory, mock_databento):
    """Seed an active IB alias first, then bootstrap via Databento for the
    same raw_symbol + asset_class — the result must surface
    ``live_qualified=True``. This pins the two-step graduation contract:
    Databento registers; IB-confirmed-live flag rides along."""
    from msai.services.nautilus.security_master.service import SecurityMaster

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )

    # ARRANGE: seed an IB alias for AAPL (exchange_name venue format).
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="NASDAQ",
            routing_venue="SMART",
            asset_class="equity",
            alias_string="AAPL.NASDAQ",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    # ACT: bootstrap via Databento.
    results = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)

    # VERIFY: live_qualified=True because an active IB alias exists.
    assert results[0].outcome == BootstrapOutcome.CREATED
    assert results[0].live_qualified is True


@pytest.mark.asyncio
async def test_live_qualified_false_when_ib_alias_is_wrong_asset_class(
    session_factory, mock_databento
):
    """Cross-asset-class symbol collision regression (Codex P2-1).

    If an IB alias exists for ``ES`` under ``asset_class=futures`` but
    we bootstrap ``ES`` as an equity via Databento, ``live_qualified``
    must stay ``False`` — the futures IB alias does not qualify the
    equity for live trading."""
    from msai.services.nautilus.security_master.service import SecurityMaster

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )

    # ARRANGE: seed an IB alias under a DIFFERENT asset_class (futures)
    # for an AAPL raw_symbol that we'll bootstrap as equity.
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)
        await sm._upsert_definition_and_alias(
            raw_symbol="AAPL",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",  # <-- wrong asset class for the equity bootstrap
            alias_string="AAPL.CME",
            provider="interactive_brokers",
            venue_format="exchange_name",
        )
        await session.commit()

    # ACT
    results = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)

    # VERIFY: live_qualified must be FALSE — the futures IB alias does
    # NOT qualify the equity bootstrap for live trading.
    assert results[0].registered is True
    assert results[0].asset_class == "equity"
    assert results[0].live_qualified is False


@pytest.mark.asyncio
async def test_ambiguous_then_exact_id_resolves_to_single_candidate(
    session_factory,
    mock_databento,
):
    """End-to-end: first POST → ambiguous with candidates[]; second POST
    with exact_ids={SYMBOL: chosen_alias} → created with canonical_id
    post-normalization.

    Closes the gap that exact_id dispatch had no integration coverage
    in iter-2/3 plan reviews."""
    from tests.integration.conftest_databento import _make_equity_instrument

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )

    # First pass: BRK.B is ambiguous (mock fixture default returns ambiguity)
    first = await svc.bootstrap(symbols=["BRK.B"], asset_class_override=None, exact_ids=None)
    assert first[0].outcome == BootstrapOutcome.AMBIGUOUS
    chosen = first[0].candidates[0]["alias_string"]  # "BRK.B.XNYS"

    # Second pass: reconfigure mock to return ONLY the chosen candidate
    # (simulates fetch_definition_instruments' exact_id pre-filter producing
    # a single match).
    mock_databento.fetch_definition_instruments = AsyncMock(
        return_value=[
            _make_equity_instrument("BRK.B", "XNYS"),
        ]
    )

    second = await svc.bootstrap(
        symbols=["BRK.B"],
        asset_class_override=None,
        exact_ids={"BRK.B": chosen},
    )
    assert second[0].outcome == BootstrapOutcome.CREATED
    assert second[0].registered is True
    # canonical_id is POST-normalization (XNYS → NYSE per venue map).
    assert second[0].canonical_id == "BRK.B.NYSE"
