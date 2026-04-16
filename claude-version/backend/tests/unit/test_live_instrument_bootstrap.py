"""Unit tests for the Phase 1 IB instrument bootstrap (Phase 1 task 1.4).

The Phase 1 live trading subprocess MUST pre-load every instrument it
will subscribe to before the run starts (Nautilus gotcha #9 — instrument
not pre-loaded fails at runtime, not startup; and #11 — never load on
the critical path). This module returns an
``InteractiveBrokersInstrumentProviderConfig`` with the contracts hard-
coded for the Phase 1 paper symbols. Phase 2 replaces this with the
full SecurityMaster lookup.
"""

from __future__ import annotations

from datetime import date

import pytest

from msai.services.nautilus.live_instrument_bootstrap import (
    PHASE_1_PAPER_SYMBOLS,
    _current_quarterly_expiry,
    build_ib_instrument_provider_config,
    canonical_instrument_id,
)


class TestPhase1PaperSymbols:
    def test_known_symbols_present(self) -> None:
        """The two Phase 1 paper symbols (AAPL, MSFT) MUST be in the catalog.
        These are the smoke-test universe for the live-supervisor."""
        assert "AAPL" in PHASE_1_PAPER_SYMBOLS
        assert "MSFT" in PHASE_1_PAPER_SYMBOLS

    def test_contract_shape_for_aapl(self) -> None:
        """AAPL contract must be a NASDAQ-routed common stock with USD."""
        contract = PHASE_1_PAPER_SYMBOLS["AAPL"]
        assert contract.symbol == "AAPL"
        assert contract.secType == "STK"
        assert contract.exchange == "SMART"
        assert contract.primaryExchange == "NASDAQ"
        assert contract.currency == "USD"


class TestBuildIBInstrumentProviderConfig:
    def test_single_symbol_loads_one_contract(self) -> None:
        config = build_ib_instrument_provider_config(["AAPL"])
        assert len(config.load_contracts) == 1
        # The contract must be the AAPL one we registered, not a freshly-built copy
        loaded = next(iter(config.load_contracts))
        assert loaded.symbol == "AAPL"
        assert loaded.secType == "STK"

    def test_multiple_symbols_load_all_contracts(self) -> None:
        config = build_ib_instrument_provider_config(["AAPL", "MSFT"])
        assert len(config.load_contracts) == 2
        symbols_in_config = {c.symbol for c in config.load_contracts}
        assert symbols_in_config == {"AAPL", "MSFT"}

    def test_unknown_symbol_raises_value_error(self) -> None:
        """An unknown symbol must fail loudly at config-build time, NOT
        at strategy startup time. Phase 1 has a closed universe — anything
        outside it is a typo or a missing config update."""
        with pytest.raises(ValueError, match="not registered in PHASE_1_PAPER_SYMBOLS"):
            build_ib_instrument_provider_config(["XYZ"])

    def test_unknown_symbol_lists_known_symbols_in_message(self) -> None:
        """The error message must include the list of known symbols so an
        operator can fix the typo without grepping the source."""
        with pytest.raises(ValueError) as exc_info:
            build_ib_instrument_provider_config(["XYZ"])
        msg = str(exc_info.value)
        assert "AAPL" in msg
        assert "MSFT" in msg

    def test_partial_unknown_in_mixed_list_raises(self) -> None:
        """If any symbol in the requested list is unknown, the WHOLE
        config build fails — we never silently drop instruments."""
        with pytest.raises(ValueError):
            build_ib_instrument_provider_config(["AAPL", "XYZ"])

    def test_empty_list_returns_empty_config(self) -> None:
        """Edge case: an empty symbol list is allowed (future tasks
        may construct an instrument-less config for diagnostics) but
        load_contracts must reflect that exactly."""
        config = build_ib_instrument_provider_config([])
        assert len(config.load_contracts) == 0

    def test_config_uses_simplified_symbology(self) -> None:
        """``IB_SIMPLIFIED`` is the recommended Nautilus symbology for
        equities — keys instruments by ``SYMBOL.EXCHANGE`` rather than
        the raw IB conId. Pin it explicitly so the live and backtest
        sides agree on instrument identifiers (parity gotcha)."""
        from nautilus_trader.adapters.interactive_brokers.config import SymbologyMethod

        config = build_ib_instrument_provider_config(["AAPL"])
        assert config.symbology_method == SymbologyMethod.IB_SIMPLIFIED

    def test_config_sets_short_cache_validity(self) -> None:
        """A 1-day cache is short enough that contract changes (corporate
        actions, expiry roll) get picked up next day, but long enough
        that restarts within a session don't re-fetch from IB on every
        boot."""
        config = build_ib_instrument_provider_config(["AAPL"])
        assert config.cache_validity_days == 1

    def test_load_contracts_is_frozenset(self) -> None:
        """Nautilus expects ``load_contracts`` as a ``frozenset`` —
        anything mutable would break the msgspec.Struct hashing."""
        config = build_ib_instrument_provider_config(["AAPL", "MSFT"])
        assert isinstance(config.load_contracts, frozenset)


class TestESFuturesContract:
    """ES (E-mini S&P 500) futures contract spec must include the
    lastTradeDateOrContractMonth for front-month resolution. IB rejects
    FUT contracts without an explicit expiry as ambiguous ("Unable to
    resolve contract details"). The exchange name IBKR expects is
    ``CME`` (not ``GLOBEX``, despite the historical alias)."""

    def test_es_is_registered(self) -> None:
        assert "ES" in PHASE_1_PAPER_SYMBOLS

    def test_es_contract_shape(self) -> None:
        contract = PHASE_1_PAPER_SYMBOLS["ES"]
        assert contract.symbol == "ES"
        assert contract.secType == "FUT"
        assert contract.exchange == "CME"
        assert contract.currency == "USD"

    def test_es_has_explicit_front_month(self) -> None:
        """The front-month contract month must be set — without it IB
        rejects the contract as ambiguous. Format is YYYYMM (6 digits)."""
        contract = PHASE_1_PAPER_SYMBOLS["ES"]
        assert len(contract.lastTradeDateOrContractMonth) == 6
        assert contract.lastTradeDateOrContractMonth.isdigit()

    def test_es_front_month_is_current_or_future(self) -> None:
        """The ES entry must reference a contract month whose 3rd Friday
        hasn't passed yet. Sanity check to catch stale hardcoded values."""
        contract = PHASE_1_PAPER_SYMBOLS["ES"]
        ym = contract.lastTradeDateOrContractMonth
        year, month = int(ym[:4]), int(ym[4:])
        assert 2026 <= year <= 2030
        assert month in (3, 6, 9, 12)  # Quarterly cycle


class TestCurrentQuarterlyExpiry:
    """``_current_quarterly_expiry(today)`` returns the next quarterly
    futures expiry (Mar/Jun/Sep/Dec) as ``YYYYMM`` — whose 3rd Friday
    is on or after ``today``. Used for ES, NQ, RTY, YM.
    """

    def test_before_june_expiry_returns_june(self) -> None:
        # April 15, 2026 — June 19, 2026 (3rd Friday) is the next expiry
        assert _current_quarterly_expiry(date(2026, 4, 15)) == "202606"

    def test_day_before_march_expiry_still_march(self) -> None:
        # 3rd Friday of March 2026 is March 20; March 19 still tradable
        assert _current_quarterly_expiry(date(2026, 3, 19)) == "202603"

    def test_on_march_expiry_day_rolls_to_june(self) -> None:
        # March 20, 2026 is the 3rd Friday. ES last-trades the Thursday
        # before (March 19), so on March 20 the March contract is
        # already expired — roll to June.
        assert _current_quarterly_expiry(date(2026, 3, 20)) == "202606"

    def test_day_after_march_expiry_returns_june(self) -> None:
        # March 21, 2026 — March already expired, next is June
        assert _current_quarterly_expiry(date(2026, 3, 21)) == "202606"

    def test_early_in_quarter_month_still_that_quarter(self) -> None:
        # June 1, 2026 — still June (expiry is June 19)
        assert _current_quarterly_expiry(date(2026, 6, 1)) == "202606"

    def test_late_december_rolls_to_next_year_march(self) -> None:
        # December 20, 2026 — December already expired (3rd Fri = Dec 18)
        assert _current_quarterly_expiry(date(2026, 12, 20)) == "202703"

    def test_january_in_new_year_returns_march(self) -> None:
        # January 15, 2027 — March 2027 is the next quarterly expiry
        assert _current_quarterly_expiry(date(2027, 1, 15)) == "202703"

    def test_non_quarterly_month_returns_next_quarterly(self) -> None:
        # February, April, May, etc. — return the next quarterly month
        assert _current_quarterly_expiry(date(2026, 2, 1)) == "202603"
        assert _current_quarterly_expiry(date(2026, 5, 1)) == "202606"
        assert _current_quarterly_expiry(date(2026, 7, 1)) == "202609"
        assert _current_quarterly_expiry(date(2026, 10, 1)) == "202612"

    def test_result_is_6_digit_string(self) -> None:
        result = _current_quarterly_expiry(date(2026, 4, 15))
        assert isinstance(result, str)
        assert len(result) == 6
        assert result.isdigit()


class TestCanonicalInstrumentId:
    """``canonical_instrument_id`` maps user-facing symbols/IDs to the
    concrete Nautilus instrument_id that will exist in the cache after
    ``build_ib_instrument_provider_config`` preloads the matching
    IBContract. Identity for stocks/ETF/FX, front-month lookup for
    futures.
    """

    def test_es_deterministic_with_today_param(self) -> None:
        """Passing an explicit ``today`` makes ES canonicalization
        deterministic — no wall-clock dependency. April 15, 2026 →
        June 2026 contract ``ESM6.CME`` (IB_SIMPLIFIED uses ``CME``
        verbatim, not the ISO MIC ``XCME``)."""
        assert canonical_instrument_id("ES.CME", today=date(2026, 4, 15)) == "ESM6.CME"

    def test_es_rolls_to_september_after_june_expiry(self) -> None:
        """June 20, 2026 (day after 3rd Friday) rolls to September."""
        assert canonical_instrument_id("ES", today=date(2026, 6, 20)) == "ESU6.CME"

    def test_es_december_maps_to_z_code(self) -> None:
        assert canonical_instrument_id("ES.CME", today=date(2026, 10, 1)) == "ESZ6.CME"

    def test_es_march_maps_to_h_code_next_year(self) -> None:
        assert canonical_instrument_id("ES", today=date(2027, 1, 15)) == "ESH7.CME"

    def test_es_legacy_xcme_input_still_accepted(self) -> None:
        """Legacy MIC-style input (``ES.XCME``) still accepted — the
        root extraction splits on ``.`` so the venue is ignored. Output
        is always ``.CME`` (IB_SIMPLIFIED venue)."""
        assert canonical_instrument_id("ES.XCME", today=date(2026, 4, 15)) == "ESM6.CME"

    def test_aapl_round_trips_identity(self) -> None:
        assert canonical_instrument_id("AAPL") == "AAPL.NASDAQ"
        assert canonical_instrument_id("AAPL.NASDAQ") == "AAPL.NASDAQ"

    def test_msft_round_trips_identity(self) -> None:
        assert canonical_instrument_id("MSFT") == "MSFT.NASDAQ"
        assert canonical_instrument_id("MSFT.NASDAQ") == "MSFT.NASDAQ"

    def test_spy_maps_to_arca(self) -> None:
        assert canonical_instrument_id("SPY") == "SPY.ARCA"
        assert canonical_instrument_id("SPY.ARCA") == "SPY.ARCA"

    def test_eur_usd_maps_to_idealpro(self) -> None:
        assert canonical_instrument_id("EUR/USD") == "EUR/USD.IDEALPRO"
        assert canonical_instrument_id("EUR/USD.IDEALPRO") == "EUR/USD.IDEALPRO"

    def test_es_maps_to_front_month_with_cme_venue(self) -> None:
        """ES is the interesting case — user writes ``ES.CME`` but
        Nautilus registers the concrete month (``ESM6.CME`` this
        quarter) because the IB adapter parses ``localSymbol``.
        Venue stays ``CME`` (IB_SIMPLIFIED, verified live 2026-04-16)."""
        result = canonical_instrument_id("ES.CME")
        # Format: ES{MonthCode}{YearLastDigit}.CME
        assert result.endswith(".CME")
        assert result.startswith("ES")
        # Month code is H/M/U/Z (quarterly); year digit is 0-9
        assert len(result) == len("ESM6.CME")
        month_code = result[2]
        assert month_code in {"H", "M", "U", "Z"}
        year_digit = result[3]
        assert year_digit.isdigit()

    def test_es_from_bare_symbol_matches_full_id(self) -> None:
        """Accepts either bare root (``"ES"``) or full id (``"ES.CME"``)
        — operator input may arrive either way."""
        assert canonical_instrument_id("ES") == canonical_instrument_id("ES.CME")

    def test_unknown_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown instrument root"):
            canonical_instrument_id("XYZ")

    def test_unknown_symbol_lists_supported_in_message(self) -> None:
        """The error message must cite the supported roots so operators
        can fix typos without grepping the source."""
        with pytest.raises(ValueError) as exc_info:
            canonical_instrument_id("XYZ.NASDAQ")
        msg = str(exc_info.value)
        assert "AAPL" in msg
        assert "ES" in msg


class TestPhaseOneSymbolsFreshPerCall:
    """``phase_1_paper_symbols()`` returns a fresh dict on every call so
    the ES front-month contract doesn't stale when a worker runs across
    a quarterly roll. Stocks/ETF/FX share stable contracts across calls.
    """

    def test_build_config_regenerates_es_contract(self) -> None:
        """Every call to ``build_ib_instrument_provider_config`` should
        produce a fresh ES contract — the contract object identity
        differs between calls even though the expiry string matches."""
        config_1 = build_ib_instrument_provider_config(["ES"])
        config_2 = build_ib_instrument_provider_config(["ES"])
        es_1 = next(iter(config_1.load_contracts))
        es_2 = next(iter(config_2.load_contracts))
        # Same logical expiry (same quarter)
        assert es_1.lastTradeDateOrContractMonth == es_2.lastTradeDateOrContractMonth
        # But different object instances — proves regeneration
        assert es_1 is not es_2

    def test_today_param_agrees_between_canonical_and_provider(self) -> None:
        """Critical invariant: when the same ``today`` is passed to both
        ``canonical_instrument_id`` and ``build_ib_instrument_provider_config``,
        the resulting Nautilus id and the IB contract expiry must
        reference the SAME quarterly contract. This is what prevents
        the supervisor/subprocess midnight-on-roll-day race."""
        shared_today = date(2026, 6, 20)  # day after June expiry
        canonical = canonical_instrument_id("ES.CME", today=shared_today)
        config = build_ib_instrument_provider_config(["ES"], today=shared_today)
        es_contract = next(iter(config.load_contracts))
        # canonical should be ESU6.CME (September)
        assert canonical == "ESU6.CME"
        # Contract expiry should also be 202609
        assert es_contract.lastTradeDateOrContractMonth == "202609"
