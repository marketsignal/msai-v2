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

import pytest

from msai.services.nautilus.live_instrument_bootstrap import (
    PHASE_1_PAPER_SYMBOLS,
    build_ib_instrument_provider_config,
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
