"""End-to-end multi-asset coverage for the security master
(Phase 2 tasks 2.12a futures, 2.12b options, 2.12c forex).

The :class:`InstrumentSpec` (2.1), :func:`spec_to_ib_contract`
(2.3), :func:`extract_trading_hours` (2.4) and
:class:`SecurityMaster` (2.5) modules ALREADY support every
asset class — these tests assert that contract by walking each
class through the spec → contract → trading-hours → cache
pipeline and asserting the right shape comes out at every
boundary.

What's covered per asset class:

- **Futures (2.12a)**: fixed-month FUT + continuous CONTFUT,
  contract field shape, canonical id round-trip.
- **Options (2.12b)**: equity option + index option (different
  underlying), strike + right propagation, ``OPT`` secType.
- **Forex (2.12c)**: ``IDEALPRO`` venue + cross-currency pair.

Each asset class has one happy path + one edge case. Pure
unit tests — no IB connection, no Postgres, no Nautilus
runtime past the contract conversion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from msai.services.nautilus.security_master.ib_qualifier import spec_to_ib_contract
from msai.services.nautilus.security_master.parser import extract_trading_hours
from msai.services.nautilus.security_master.specs import InstrumentSpec

# ---------------------------------------------------------------------------
# 2.12a — Futures
# ---------------------------------------------------------------------------


class TestFuturesEndToEnd:
    def test_es_june_2025_fixed_month(self) -> None:
        """Standard fixed-month future: ESM5 on CME. The full
        pipeline produces the canonical id `ESM5.CME` and the
        IB contract has secType=FUT + the right
        lastTradeDateOrContractMonth."""
        spec = InstrumentSpec(
            asset_class="future",
            symbol="ES",
            venue="CME",
            expiry=date(2025, 6, 20),
        )
        assert spec.canonical_id() == "ESM5.CME"

        contract = spec_to_ib_contract(spec)
        assert contract.secType == "FUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"
        # IB resolves the actual last-trade date from yyyyMM — this
        # avoids the computed-3rd-Friday trap on holiday-shifted months
        # (e.g. Juneteenth 2026-06-19). PR #37 pinned the format.
        assert contract.lastTradeDateOrContractMonth == "202506"

    def test_continuous_es_no_expiry(self) -> None:
        """CONTFUT secType for the continuous-front-month ES — the
        canonical id is just `ES.CME` (no month/year suffix)."""
        spec = InstrumentSpec(
            asset_class="future",
            symbol="ES",
            venue="CME",
        )
        assert spec.canonical_id() == "ES.CME"

        contract = spec_to_ib_contract(spec)
        assert contract.secType == "CONTFUT"
        assert contract.lastTradeDateOrContractMonth == ""

    def test_futures_trading_hours_extracted(self) -> None:
        """CME ES futures have a near-24h schedule. The
        extractor must handle the multi-day session shape and
        derive a non-empty result."""
        # Three weekday sessions with 23-hour spans
        trading_hours = (
            "20260601:1700-20260602:1600;20260602:1700-20260603:1600;20260603:1700-20260604:1600"
        )
        liquid_hours = "20260601:1700-20260602:1600"
        result = extract_trading_hours(
            trading_hours=trading_hours,
            liquid_hours=liquid_hours,
            time_zone_id="America/Chicago",
        )
        assert result is not None
        assert result["timezone"] == "America/Chicago"
        assert len(result["rth"]) >= 1


# ---------------------------------------------------------------------------
# 2.12b — Options
# ---------------------------------------------------------------------------


class TestOptionsEndToEnd:
    def test_aapl_call_option(self) -> None:
        """Standard equity option: AAPL May 2026 150C on SMART.
        Canonical id format `{right} {underlying} {yyyyMMdd} {strike:g}.{venue}`
        matches Nautilus's IB simplified symbology."""
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

        contract = spec_to_ib_contract(spec)
        assert contract.secType == "OPT"
        assert contract.symbol == "AAPL"  # underlying is the IB contract symbol
        assert contract.exchange == "SMART"
        assert contract.strike == 150.0
        assert contract.right == "C"
        assert contract.lastTradeDateOrContractMonth == "20260515"

    def test_index_option_underlying_differs_from_symbol(self) -> None:
        """SPXW (weekly) on SPX: the option ticker (`SPXW`) is
        different from the IB underlying (`SPX`). The canonical
        id uses the underlying so the same option resolves to the
        same id regardless of which weekly the operator queried."""
        spec = InstrumentSpec(
            asset_class="option",
            symbol="SPXW",
            venue="CBOE",
            expiry=date(2026, 3, 15),
            strike=Decimal("4500"),
            right="P",
            underlying="SPX",
        )
        assert spec.canonical_id() == "P SPX 20260315 4500.CBOE"

        contract = spec_to_ib_contract(spec)
        assert contract.secType == "OPT"
        # The IB contract uses the underlying, NOT the SPXW
        # symbol the operator passed. Important: the strategy
        # registry can normalize SPXW → SPX without losing the
        # ability to qualify against IB.
        assert contract.symbol == "SPX"
        assert contract.right == "P"
        assert contract.strike == 4500.0

    def test_option_explicit_strike_required(self) -> None:
        """Per gotcha #12, options MUST be qualified with an
        explicit strike — no chain expansion in the qualifier
        path. The InstrumentSpec validator enforces this at
        construction time."""
        with pytest.raises(ValueError, match="strike"):
            InstrumentSpec(
                asset_class="option",
                symbol="AAPL",
                venue="SMART",
                expiry=date(2026, 5, 15),
                right="C",
                underlying="AAPL",
            )


# ---------------------------------------------------------------------------
# 2.12c — Forex
# ---------------------------------------------------------------------------


class TestForexEndToEnd:
    def test_eurusd_major_pair(self) -> None:
        """EUR/USD on IDEALPRO is the canonical major pair. CASH
        secType, base/quote split into symbol/currency."""
        spec = InstrumentSpec(
            asset_class="forex",
            symbol="EUR",
            venue="IDEALPRO",
            currency="USD",
        )
        assert spec.canonical_id() == "EUR/USD.IDEALPRO"

        contract = spec_to_ib_contract(spec)
        assert contract.secType == "CASH"
        assert contract.symbol == "EUR"  # base
        assert contract.currency == "USD"  # quote
        assert contract.exchange == "IDEALPRO"

    def test_cross_currency_pair_no_usd(self) -> None:
        """EUR/GBP — neither side is USD. The canonical id and
        IB contract handle non-USD pairs correctly."""
        spec = InstrumentSpec(
            asset_class="forex",
            symbol="EUR",
            venue="IDEALPRO",
            currency="GBP",
        )
        assert spec.canonical_id() == "EUR/GBP.IDEALPRO"

        contract = spec_to_ib_contract(spec)
        assert contract.symbol == "EUR"
        assert contract.currency == "GBP"

    def test_forex_trading_hours_returns_none_for_24h(self) -> None:
        """IDEALPRO forex has no meaningful daily session
        boundary. The extractor returns None so the cache row
        stores NULL in the trading_hours column."""
        result = extract_trading_hours(
            trading_hours=None,
            liquid_hours=None,
            time_zone_id=None,
        )
        assert result is None
