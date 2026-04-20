from datetime import date

import pytest

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.live_resolver import (
    RegistryIncompleteError,
    _build_contract_spec,
)


def _make_definition(**overrides: object) -> InstrumentDefinition:
    base: dict[str, object] = dict(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="SMART",
        asset_class="equity",
        provider="interactive_brokers",
    )
    base.update(overrides)
    return InstrumentDefinition(**base)


def _make_alias(alias_string: str, effective_from: date) -> InstrumentAlias:
    return InstrumentAlias(
        alias_string=alias_string,
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=effective_from,
    )


def test_equity_contract_spec() -> None:
    spec = _build_contract_spec(
        _make_definition(),
        _make_alias("AAPL.NASDAQ", date(2026, 1, 1)),
    )
    assert spec == {
        "secType": "STK",
        "symbol": "AAPL",
        "exchange": "SMART",
        "primaryExchange": "NASDAQ",
        "currency": "USD",
    }


def test_etf_contract_spec_uses_arca_primary() -> None:
    spec = _build_contract_spec(
        _make_definition(raw_symbol="SPY", listing_venue="ARCA"),
        _make_alias("SPY.ARCA", date(2026, 1, 1)),
    )
    assert spec["primaryExchange"] == "ARCA"
    assert spec["secType"] == "STK"


def test_fx_contract_spec() -> None:
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="EUR/USD",
            listing_venue="IDEALPRO",
            routing_venue="IDEALPRO",
            asset_class="fx",
        ),
        _make_alias("EUR/USD.IDEALPRO", date(2026, 1, 1)),
    )
    assert spec == {
        "secType": "CASH",
        "symbol": "EUR",
        "exchange": "IDEALPRO",
        "currency": "USD",
    }


def test_futures_contract_spec_parses_alias_string() -> None:
    """ES alias 'ESM6.CME' -> lastTradeDateOrContractMonth='202606'."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESM6.CME", date(2026, 3, 20)),
    )
    assert spec == {
        "secType": "FUT",
        "symbol": "ES",
        "exchange": "CME",
        "lastTradeDateOrContractMonth": "202606",
        "currency": "USD",
    }


def test_futures_contract_spec_z5_december_2025() -> None:
    """Z = December, 5 = 2025 (year inferred from effective_from decade)."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="NQ",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("NQZ5.CME", date(2025, 9, 1)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "202512"


def test_futures_decade_boundary_forward() -> None:
    """effective_from=2029-12-15 + alias ESH0.CME -> March 2030, not 2020."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESH0.CME", date(2029, 12, 15)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "203003"


def test_futures_in_decade_uses_current_year_not_next() -> None:
    """effective_from=2026-01-01 + alias ESM6.CME -> 2026-06, not 2036."""
    spec = _build_contract_spec(
        _make_definition(
            raw_symbol="ES",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
        ),
        _make_alias("ESM6.CME", date(2026, 1, 1)),
    )
    assert spec["lastTradeDateOrContractMonth"] == "202606"


def test_equity_missing_listing_venue_raises_incomplete() -> None:
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(listing_venue=""),
            _make_alias("AAPL.NASDAQ", date(2026, 1, 1)),
        )
    assert excinfo.value.missing_field == "listing_venue"


def test_fx_raw_symbol_without_slash_raises_incomplete() -> None:
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(raw_symbol="EURUSD", asset_class="fx"),
            _make_alias("EURUSD.IDEALPRO", date(2026, 1, 1)),
        )
    assert excinfo.value.missing_field == "raw_symbol.base_quote_split"


def test_fx_raw_symbol_empty_base_or_quote_raises_incomplete() -> None:
    """'EUR/' or '/USD' — base or quote empty after split."""
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(raw_symbol="EUR/", asset_class="fx"),
            _make_alias("EUR/.IDEALPRO", date(2026, 1, 1)),
        )
    assert excinfo.value.missing_field == "raw_symbol.malformed"


def test_futures_malformed_alias_raises_incomplete() -> None:
    with pytest.raises(RegistryIncompleteError) as excinfo:
        _build_contract_spec(
            _make_definition(raw_symbol="ES", asset_class="futures"),
            _make_alias("ES.CME", date(2026, 1, 1)),  # missing month code
        )
    assert "alias" in excinfo.value.missing_field
