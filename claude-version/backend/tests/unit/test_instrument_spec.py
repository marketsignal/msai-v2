"""Unit tests for :class:`InstrumentSpec` (Phase 2 task 2.1)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from msai.services.nautilus.security_master.specs import InstrumentSpec

# ---------------------------------------------------------------------------
# canonical_id() per asset class
# ---------------------------------------------------------------------------


class TestCanonicalIdEquity:
    def test_aapl_on_nasdaq(self) -> None:
        spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        assert spec.canonical_id() == "AAPL.NASDAQ"

    def test_non_usd_quote_currency_does_not_change_canonical(self) -> None:
        """``currency`` is metadata for the cache layer; the canonical
        ID only encodes ``symbol`` + ``venue`` for equities (matches
        Nautilus simplified symbology, which doesn't embed currency
        in the instrument_id)."""
        spec = InstrumentSpec(asset_class="equity", symbol="VOD", venue="LSE", currency="GBP")
        assert spec.canonical_id() == "VOD.LSE"


class TestCanonicalIdIndex:
    def test_spx_adds_caret_prefix(self) -> None:
        """Nautilus simplified symbology adds a ``^`` prefix to index
        symbols (``parsing/instruments.py`` line 1062)."""
        spec = InstrumentSpec(asset_class="index", symbol="SPX", venue="CBOE")
        assert spec.canonical_id() == "^SPX.CBOE"

    def test_caret_prefix_is_idempotent(self) -> None:
        """If the caller already supplied ``^``, don't double it."""
        spec = InstrumentSpec(asset_class="index", symbol="^SPX", venue="CBOE")
        assert spec.canonical_id() == "^SPX.CBOE"


class TestCanonicalIdFuture:
    def test_continuous_future_is_just_root(self) -> None:
        """A futures spec with ``expiry=None`` resolves to the
        continuous contract (CONTFUT) — Nautilus encodes that as
        just the root on the venue."""
        spec = InstrumentSpec(asset_class="future", symbol="ES", venue="XCME")
        assert spec.canonical_id() == "ES.XCME"

    def test_fixed_month_future_encodes_month_and_year_digit(self) -> None:
        """Fixed-month future: ``{root}{month_code}{year_digit}.{venue}``.
        For June 2025, month code ``M`` + year digit ``5`` → ``ESM5``.
        Matches Nautilus ``ib_contract_to_instrument_id_simplified_symbology``
        line 1081."""
        spec = InstrumentSpec(
            asset_class="future",
            symbol="ES",
            venue="XCME",
            expiry=date(2025, 6, 20),
        )
        assert spec.canonical_id() == "ESM5.XCME"

    def test_every_month_code_roundtrips(self) -> None:
        """Regression guard: verify all twelve futures month codes
        render correctly. Maps to the CME/ICE letter code convention
        (F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun, N=Jul, Q=Aug,
        U=Sep, V=Oct, X=Nov, Z=Dec)."""
        codes = {
            1: "F",
            2: "G",
            3: "H",
            4: "J",
            5: "K",
            6: "M",
            7: "N",
            8: "Q",
            9: "U",
            10: "V",
            11: "X",
            12: "Z",
        }
        for month, letter in codes.items():
            spec = InstrumentSpec(
                asset_class="future",
                symbol="CL",
                venue="NYMEX",
                expiry=date(2027, month, 15),
            )
            assert spec.canonical_id() == f"CL{letter}7.NYMEX"


class TestCanonicalIdOption:
    def test_aapl_call_expiry_strike(self) -> None:
        """``{right} {underlying} {yyyyMMdd} {strike:g}.{venue}`` —
        matches Nautilus simplified symbology line 1068."""
        spec = InstrumentSpec(
            asset_class="option",
            symbol="AAPL",
            venue="SMART",
            expiry=date(2026, 5, 15),
            strike=Decimal("150"),
            right="C",
            underlying="AAPL",
        )
        assert spec.canonical_id() == "C AAPL 20260515 150.SMART"

    def test_put_with_decimal_strike(self) -> None:
        """Decimal strikes should render without trailing zeros
        (``{strike:g}`` in Python's format spec drops insignificant
        trailing zeros)."""
        spec = InstrumentSpec(
            asset_class="option",
            symbol="SPY",
            venue="SMART",
            expiry=date(2026, 12, 19),
            strike=Decimal("450.5"),
            right="P",
            underlying="SPY",
        )
        assert spec.canonical_id() == "P SPY 20261219 450.5.SMART"

    def test_index_option_underlying_different_from_symbol(self) -> None:
        """Index options have ``underlying != symbol`` because the
        option ticker often includes a prefix (e.g. SPXW for SPX
        weeklies)."""
        spec = InstrumentSpec(
            asset_class="option",
            symbol="SPXW",
            venue="CBOE",
            expiry=date(2026, 3, 15),
            strike=Decimal("4500"),
            right="C",
            underlying="SPX",
        )
        assert spec.canonical_id() == "C SPX 20260315 4500.CBOE"


class TestCanonicalIdForex:
    def test_eurusd_on_idealpro(self) -> None:
        """CASH branch: ``{base}/{quote}.{venue}`` — line 1085."""
        spec = InstrumentSpec(
            asset_class="forex",
            symbol="EUR",
            venue="IDEALPRO",
            currency="USD",
        )
        assert spec.canonical_id() == "EUR/USD.IDEALPRO"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            InstrumentSpec(asset_class="equity", symbol="", venue="NASDAQ")

    def test_empty_venue_raises(self) -> None:
        with pytest.raises(ValueError, match="venue"):
            InstrumentSpec(asset_class="equity", symbol="AAPL", venue="")

    def test_option_missing_expiry_raises(self) -> None:
        with pytest.raises(ValueError, match="expiry"):
            InstrumentSpec(
                asset_class="option",
                symbol="AAPL",
                venue="SMART",
                strike=Decimal("150"),
                right="C",
                underlying="AAPL",
            )

    def test_option_missing_strike_raises(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            InstrumentSpec(
                asset_class="option",
                symbol="AAPL",
                venue="SMART",
                expiry=date(2026, 5, 15),
                right="C",
                underlying="AAPL",
            )

    def test_option_missing_right_raises(self) -> None:
        with pytest.raises(ValueError, match="right"):
            InstrumentSpec(
                asset_class="option",
                symbol="AAPL",
                venue="SMART",
                expiry=date(2026, 5, 15),
                strike=Decimal("150"),
                underlying="AAPL",
            )

    def test_option_missing_underlying_raises(self) -> None:
        with pytest.raises(ValueError, match="underlying"):
            InstrumentSpec(
                asset_class="option",
                symbol="AAPL",
                venue="SMART",
                expiry=date(2026, 5, 15),
                strike=Decimal("150"),
                right="C",
            )

    def test_equity_with_option_fields_raises(self) -> None:
        """Stray option fields on an equity spec are a programming
        error — reject at construction."""
        with pytest.raises(ValueError, match="equity"):
            InstrumentSpec(
                asset_class="equity",
                symbol="AAPL",
                venue="NASDAQ",
                strike=Decimal("150"),  # not allowed on equity
            )

    def test_index_with_option_fields_raises(self) -> None:
        with pytest.raises(ValueError, match="index"):
            InstrumentSpec(
                asset_class="index",
                symbol="SPX",
                venue="CBOE",
                right="C",  # not allowed on index
            )


# ---------------------------------------------------------------------------
# Hashability + equality (frozen dataclass contract)
# ---------------------------------------------------------------------------


class TestHashEquality:
    def test_identical_specs_are_equal(self) -> None:
        a = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        b = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        assert a == b
        assert hash(a) == hash(b)

    def test_different_venue_is_different_spec(self) -> None:
        a = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        b = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NYSE")
        assert a != b
        assert a.canonical_id() != b.canonical_id()

    def test_frozen_cannot_reassign(self) -> None:
        spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        with pytest.raises((AttributeError, TypeError)):
            spec.symbol = "MSFT"  # type: ignore[misc]
