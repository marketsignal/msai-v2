"""TIF=DAY injection for US-equity venues (Bug #2, live-deploy-safety-trio).

Verifies the `_strategy_us_equity_tif_overrides` helper that drives
the `market_exit_time_in_force="DAY"` injection in both
`build_live_trading_node_config` and `build_portfolio_trading_node_config`.
"""

from __future__ import annotations

from nautilus_trader.model.enums import TimeInForce

from msai.services.nautilus.live_node_config import (
    _has_us_equity_venue,
    _strategy_us_equity_tif_overrides,
)


class TestHasUsEquityVenue:
    def test_nasdaq_is_us_equity(self) -> None:
        assert _has_us_equity_venue(["AAPL.NASDAQ"])

    def test_nyse_is_us_equity(self) -> None:
        assert _has_us_equity_venue(["JNJ.NYSE"])

    def test_arca_is_us_equity(self) -> None:
        assert _has_us_equity_venue(["SPY.ARCA"])

    def test_case_insensitive(self) -> None:
        assert _has_us_equity_venue(["AAPL.nasdaq"])

    def test_cme_is_not_us_equity(self) -> None:
        assert not _has_us_equity_venue(["ESM4.CME"])

    def test_fx_is_not_us_equity(self) -> None:
        assert not _has_us_equity_venue(["EUR/USD.IDEALPRO"])

    def test_no_venue_is_not_us_equity(self) -> None:
        # Malformed input — caller keeps default (no override).
        assert not _has_us_equity_venue(["AAPL"])

    def test_mixed_returns_true_if_any_us_equity(self) -> None:
        assert _has_us_equity_venue(["ESM4.CME", "AAPL.NASDAQ"])

    def test_empty_list_returns_false(self) -> None:
        assert not _has_us_equity_venue([])


class TestStrategyUsEquityTifOverrides:
    def test_returns_day_for_instruments_list_with_us_equity(self) -> None:
        config = {"instruments": ["AAPL.NASDAQ"]}
        assert _strategy_us_equity_tif_overrides(config) == {
            "market_exit_time_in_force": int(TimeInForce.DAY)
        }

    def test_returns_day_for_single_instrument_id(self) -> None:
        config = {"instrument_id": "SPY.ARCA"}
        assert _strategy_us_equity_tif_overrides(config) == {
            "market_exit_time_in_force": int(TimeInForce.DAY)
        }

    def test_returns_empty_for_futures(self) -> None:
        config = {"instruments": ["ESM4.CME"]}
        assert _strategy_us_equity_tif_overrides(config) == {}

    def test_returns_empty_for_fx(self) -> None:
        config = {"instrument_id": "EUR/USD.IDEALPRO"}
        assert _strategy_us_equity_tif_overrides(config) == {}

    def test_returns_empty_for_missing_fields(self) -> None:
        assert _strategy_us_equity_tif_overrides({}) == {}

    def test_returns_empty_for_null_instrument_id(self) -> None:
        # ``"instrument_id": None`` should not crash, should not match.
        assert _strategy_us_equity_tif_overrides({"instrument_id": None}) == {}

    def test_mixed_us_equity_and_futures_returns_day(self) -> None:
        config = {"instruments": ["ESM4.CME", "AAPL.NASDAQ"]}
        assert _strategy_us_equity_tif_overrides(config) == {
            "market_exit_time_in_force": int(TimeInForce.DAY)
        }

    def test_extra_instruments_us_equity_triggers_override(self) -> None:
        """PR #65 Codex P2: portfolio members store the authoritative
        instrument list on the payload, not the config. A strategy whose
        config carries only a non-US-equity instrument_id but whose
        payload contains a US-equity member must still get TIF=DAY."""
        config = {"instrument_id": "ESM4.CME"}
        result = _strategy_us_equity_tif_overrides(config, extra_instruments=["AAPL.NASDAQ"])
        assert result == {"market_exit_time_in_force": int(TimeInForce.DAY)}

    def test_extra_instruments_no_us_equity_returns_empty(self) -> None:
        """Extra instruments that are all non-US-equity keep the GTC default."""
        config = {"instrument_id": "ESM4.CME"}
        result = _strategy_us_equity_tif_overrides(
            config, extra_instruments=["ESH5.CME", "EUR/USD.IDEALPRO"]
        )
        assert result == {}

    def test_extra_instruments_none_is_safe(self) -> None:
        """Default ``extra_instruments=None`` matches the pre-PR-#65 signature."""
        config = {"instruments": ["AAPL.NASDAQ"]}
        result = _strategy_us_equity_tif_overrides(config, extra_instruments=None)
        assert result == {"market_exit_time_in_force": int(TimeInForce.DAY)}
