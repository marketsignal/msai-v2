"""Unit tests for build_ib_instrument_provider_config_from_resolved
(Task 10 — registry-backed IB preload builder).
"""

from __future__ import annotations

from datetime import date

from msai.services.nautilus.live_instrument_bootstrap import (
    build_ib_instrument_provider_config_from_resolved,
)
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
)


def test_build_from_resolved_equity() -> None:
    resolved = [
        ResolvedInstrument(
            canonical_id="QQQ.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK",
                "symbol": "QQQ",
                "exchange": "SMART",
                "primaryExchange": "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    ]
    cfg = build_ib_instrument_provider_config_from_resolved(resolved)
    assert len(cfg.load_contracts) == 1
    contract = next(iter(cfg.load_contracts))
    assert contract.secType == "STK"
    assert contract.symbol == "QQQ"
    assert contract.primaryExchange == "NASDAQ"
    assert contract.currency == "USD"


def test_build_from_resolved_fx() -> None:
    resolved = [
        ResolvedInstrument(
            canonical_id="EUR/USD.IDEALPRO",
            asset_class=AssetClass.FX,
            contract_spec={
                "secType": "CASH",
                "symbol": "EUR",
                "exchange": "IDEALPRO",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    ]
    cfg = build_ib_instrument_provider_config_from_resolved(resolved)
    contract = next(iter(cfg.load_contracts))
    assert contract.secType == "CASH"
    assert contract.symbol == "EUR"
    assert contract.currency == "USD"


def test_build_from_resolved_futures_includes_expiry() -> None:
    resolved = [
        ResolvedInstrument(
            canonical_id="ESM6.CME",
            asset_class=AssetClass.FUTURES,
            contract_spec={
                "secType": "FUT",
                "symbol": "ES",
                "exchange": "CME",
                "lastTradeDateOrContractMonth": "202606",
                "currency": "USD",
            },
            effective_window=(date(2026, 3, 20), date(2026, 6, 20)),
        ),
    ]
    cfg = build_ib_instrument_provider_config_from_resolved(resolved)
    contract = next(iter(cfg.load_contracts))
    assert contract.secType == "FUT"
    assert contract.symbol == "ES"
    assert contract.lastTradeDateOrContractMonth == "202606"


def test_build_from_resolved_empty_list_produces_empty_config() -> None:
    """Empty resolved list is acceptable here — Task 11's aggregator
    is the layer that enforces 'at least one instrument'."""
    cfg = build_ib_instrument_provider_config_from_resolved([])
    assert len(cfg.load_contracts) == 0


def test_build_from_resolved_ignores_unknown_contract_spec_keys() -> None:
    """Forward-compat: options extension will add expiry/strike/right
    to contract_spec. The IB adapter rejects unknown kwargs, so the
    builder filters to the whitelist of known IB fields."""
    resolved = [
        ResolvedInstrument(
            canonical_id="AAPL.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK",
                "symbol": "AAPL",
                "exchange": "SMART",
                "primaryExchange": "NASDAQ",
                "currency": "USD",
                "future_field_not_yet_in_IB_API": "ignored",
            },
            effective_window=(date(2026, 1, 1), None),
        ),
    ]
    # Must not raise even with unknown keys
    cfg = build_ib_instrument_provider_config_from_resolved(resolved)
    assert len(cfg.load_contracts) == 1
