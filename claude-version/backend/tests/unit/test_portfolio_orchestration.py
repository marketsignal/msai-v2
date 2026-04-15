"""Unit tests for portfolio orchestration helpers.

Covers the pure functions in :mod:`msai.services.portfolio_service`:
``_heuristic_weight``, ``_effective_leverage``,
``_load_benchmark_returns``, ``_extract_returns_from_account``.

The orchestration DAG (``run_portfolio_backtest`` and friends) is covered
by the integration test at
``tests/integration/test_portfolio_job_orchestration.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from msai.models.portfolio_enums import PortfolioObjective
from msai.services.portfolio_service import (
    PortfolioOrchestrationError,
    _coerce_objective,
    _effective_leverage,
    _extract_returns_from_account,
    _heuristic_weight,
    _load_benchmark_returns,
    _prepare_strategy_config,
    _raw_benchmark_symbol,
)

# ---------------------------------------------------------------------------
# _coerce_objective
# ---------------------------------------------------------------------------


class TestCoerceObjective:
    def test_accepts_enum_directly(self) -> None:
        assert (
            _coerce_objective(PortfolioObjective.MAXIMIZE_SHARPE)
            is PortfolioObjective.MAXIMIZE_SHARPE
        )

    def test_accepts_string_value(self) -> None:
        assert _coerce_objective("maximize_sharpe") is PortfolioObjective.MAXIMIZE_SHARPE

    def test_translates_legacy_max_sharpe(self) -> None:
        # Existing rows were created with "max_sharpe" before the rename;
        # orchestration must keep working for them.
        assert _coerce_objective("max_sharpe") is PortfolioObjective.MAXIMIZE_SHARPE

    def test_unknown_string_raises(self) -> None:
        with pytest.raises(PortfolioOrchestrationError, match="Unknown portfolio objective"):
            _coerce_objective("maximise_alpha")  # typo intentional

    def test_non_string_raises(self) -> None:
        with pytest.raises(PortfolioOrchestrationError, match="Unexpected"):
            _coerce_objective(42)


# ---------------------------------------------------------------------------
# _heuristic_weight
# ---------------------------------------------------------------------------


class TestHeuristicWeight:
    def test_maximize_sharpe_uses_sharpe_metric(self) -> None:
        weight = _heuristic_weight({"sharpe": 1.5}, PortfolioObjective.MAXIMIZE_SHARPE)
        assert weight == 1.5

    def test_maximize_sharpe_floors_negative_at_unity(self) -> None:
        # Negative sharpe -> floor to 1.0 so the candidate survives to
        # normalization (which will proportionally down-weight it).
        weight = _heuristic_weight({"sharpe": -0.3}, PortfolioObjective.MAXIMIZE_SHARPE)
        assert weight == 1.0

    def test_maximize_sortino_uses_sortino_metric(self) -> None:
        assert _heuristic_weight({"sortino": 2.0}, PortfolioObjective.MAXIMIZE_SORTINO) == 2.0

    def test_maximize_profit_uses_total_return(self) -> None:
        assert _heuristic_weight({"total_return": 0.35}, PortfolioObjective.MAXIMIZE_PROFIT) == 0.35

    def test_equal_weight_always_returns_unity(self) -> None:
        assert _heuristic_weight({"sharpe": 5.0}, PortfolioObjective.EQUAL_WEIGHT) == 1.0

    def test_manual_returns_unity(self) -> None:
        # Manual objective means explicit weights are required; heuristic
        # is a safe "equal" default when that contract is bypassed.
        assert _heuristic_weight({}, PortfolioObjective.MANUAL) == 1.0

    def test_missing_metric_falls_back_to_unity(self) -> None:
        assert _heuristic_weight({}, PortfolioObjective.MAXIMIZE_SHARPE) == 1.0

    def test_none_metric_falls_back_to_unity(self) -> None:
        assert _heuristic_weight({"sharpe": None}, PortfolioObjective.MAXIMIZE_SHARPE) == 1.0


# ---------------------------------------------------------------------------
# _effective_leverage
# ---------------------------------------------------------------------------


class TestEffectiveLeverage:
    def _series(self, values: list[float]) -> pd.Series:
        return pd.Series(
            values,
            index=pd.date_range("2024-01-01", periods=len(values), freq="D"),
        )

    def test_no_downside_target_returns_requested_leverage(self) -> None:
        weighted = [("s", 1.0, self._series([0.01, -0.02, 0.03]))]
        assert (
            _effective_leverage(
                weighted_series=weighted,
                requested_leverage=2.0,
                downside_target=None,
            )
            == 2.0
        )

    def test_zero_downside_target_disables_scaling(self) -> None:
        # Pydantic rejects <=0 at the API boundary, but defensively accept
        # it here and pass requested_leverage through unchanged.
        weighted = [("s", 1.0, self._series([0.01, -0.02]))]
        assert (
            _effective_leverage(
                weighted_series=weighted,
                requested_leverage=1.5,
                downside_target=0.0,
            )
            == 1.5
        )

    def test_zero_requested_leverage_preserved(self) -> None:
        # A requested 0x leverage is an explicit choice — respect it
        # rather than silently upgrading to 1.0.
        weighted = [("s", 1.0, self._series([0.01, -0.02]))]
        assert (
            _effective_leverage(
                weighted_series=weighted,
                requested_leverage=0.0,
                downside_target=0.05,
            )
            == 0.0
        )

    def test_high_downside_scales_leverage_down(self) -> None:
        # Large losses -> high downside risk -> leverage must scale down.
        weighted = [("s", 1.0, self._series([-0.05, -0.08, -0.06, -0.04]))]
        lev = _effective_leverage(
            weighted_series=weighted,
            requested_leverage=2.0,
            downside_target=0.05,
        )
        assert 0.1 <= lev < 2.0

    def test_zero_downside_risk_preserves_requested_leverage(self) -> None:
        # All-positive series -> downside_risk is 0 -> leverage passes through.
        weighted = [("s", 1.0, self._series([0.01, 0.02, 0.015]))]
        lev = _effective_leverage(
            weighted_series=weighted,
            requested_leverage=3.0,
            downside_target=0.05,
        )
        assert lev == 3.0

    def test_never_scales_below_safety_floor(self) -> None:
        # Pathological downside -> still clamp to 0.1 minimum.
        weighted = [("s", 1.0, self._series([-0.5, -0.6, -0.7]))]
        lev = _effective_leverage(
            weighted_series=weighted,
            requested_leverage=1.0,
            downside_target=0.001,  # extremely tight target
        )
        assert lev >= 0.1


# ---------------------------------------------------------------------------
# _load_benchmark_returns
# ---------------------------------------------------------------------------


class TestLoadBenchmarkReturns:
    def test_empty_symbol_returns_none(self) -> None:
        mq = MagicMock()
        assert (
            _load_benchmark_returns(
                mq, benchmark_symbol=None, start_date="2024-01-01", end_date="2024-01-31"
            )
            is None
        )
        assert (
            _load_benchmark_returns(
                mq, benchmark_symbol="", start_date="2024-01-01", end_date="2024-01-31"
            )
            is None
        )
        mq.get_bars.assert_not_called()

    def test_tries_full_symbol_first_then_strips(self) -> None:
        # First call: full symbol (e.g. ``SPY.NASDAQ``).  Second call
        # (fallback): last segment stripped.  Both are observed so the
        # caller can see the try-then-strip sequence explicitly.
        mq = MagicMock()
        mq.get_bars.side_effect = [[], []]  # both return empty
        _load_benchmark_returns(
            mq,
            benchmark_symbol="SPY.NASDAQ",
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
        symbols_tried = [call.args[0] for call in mq.get_bars.call_args_list]
        assert symbols_tried == ["SPY.NASDAQ", "SPY"]

    def test_preserves_share_class_dots(self) -> None:
        # ``BRK.B`` — share class with a dot.  Full-symbol lookup hits,
        # so the fallback is never tried.
        mq = MagicMock()
        mq.get_bars.return_value = [
            {"timestamp": "2024-01-01T00:00:00Z", "close": 100.0},
            {"timestamp": "2024-01-02T00:00:00Z", "close": 101.0},
        ]
        result = _load_benchmark_returns(
            mq,
            benchmark_symbol="BRK.B",
            start_date="2024-01-01",
            end_date="2024-01-02",
        )
        assert result is not None
        # Only one call — no fallback needed because the first hit.
        assert mq.get_bars.call_count == 1
        assert mq.get_bars.call_args.args[0] == "BRK.B"

    def test_coerces_bad_timestamp_to_none(self) -> None:
        # Malformed timestamp must degrade to None — benchmark is
        # optional and must not abort the portfolio run.
        mq = MagicMock()
        mq.get_bars.return_value = [
            {"timestamp": "not-a-date", "close": 100.0},
            {"timestamp": "also-bad", "close": 101.0},
        ]
        result = _load_benchmark_returns(
            mq,
            benchmark_symbol="SPY",
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
        assert result is None

    def test_empty_bars_returns_none(self) -> None:
        mq = MagicMock()
        mq.get_bars.return_value = []
        result = _load_benchmark_returns(
            mq,
            benchmark_symbol="SPY",
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
        assert result is None

    def test_computes_pct_change_from_daily_close(self) -> None:
        # Intraday bars are resampled to daily close before pct_change.
        mq = MagicMock()
        mq.get_bars.return_value = [
            {"timestamp": "2024-01-01T15:00:00Z", "close": 100.0},
            {"timestamp": "2024-01-02T15:00:00Z", "close": 101.0},
            {"timestamp": "2024-01-03T15:00:00Z", "close": 102.01},
        ]
        result = _load_benchmark_returns(
            mq,
            benchmark_symbol="SPY",
            start_date="2024-01-01",
            end_date="2024-01-03",
        )
        assert result is not None
        # 3 daily bars → 3 daily returns (first is zero from pct_change NaN).
        assert len(result) == 3
        assert result.iloc[0] == 0.0
        assert abs(result.iloc[1] - 0.01) < 1e-9
        assert abs(result.iloc[2] - 0.01) < 1e-6

    def test_missing_columns_returns_none(self) -> None:
        mq = MagicMock()
        mq.get_bars.return_value = [{"timestamp": "2024-01-01T00:00:00Z"}]
        assert (
            _load_benchmark_returns(
                mq, benchmark_symbol="SPY", start_date="2024-01-01", end_date="2024-01-31"
            )
            is None
        )


# ---------------------------------------------------------------------------
# _extract_returns_from_account
# ---------------------------------------------------------------------------


class TestExtractReturnsFromAccount:
    def test_empty_frame_returns_empty_lists(self) -> None:
        returns, timestamps = _extract_returns_from_account(pd.DataFrame())
        assert returns == []
        assert timestamps == []

    def test_prefers_returns_column(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2024-01-01", "2024-01-02"],
                "returns": [0.01, -0.02],
            }
        )
        returns, timestamps = _extract_returns_from_account(frame)
        assert returns == pytest.approx([0.01, -0.02])
        assert len(timestamps) == 2

    def test_falls_back_to_equity_pct_change(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2024-01-01", "2024-01-02", "2024-01-03"],
                "equity": [100.0, 101.0, 100.0],
            }
        )
        returns, timestamps = _extract_returns_from_account(frame)
        assert len(returns) == 3
        assert returns[0] == 0.0
        assert abs(returns[1] - 0.01) < 1e-9
        assert abs(returns[2] - (-1 / 101)) < 1e-9

    def test_falls_back_to_net_liquidation(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2024-01-01", "2024-01-02"],
                "net_liquidation": [1000.0, 1050.0],
            }
        )
        returns, _ = _extract_returns_from_account(frame)
        assert returns[0] == 0.0
        assert abs(returns[1] - 0.05) < 1e-9

    def test_no_usable_column_returns_empty(self) -> None:
        frame = pd.DataFrame({"timestamp": ["2024-01-01"], "garbage": [1]})
        returns, timestamps = _extract_returns_from_account(frame)
        assert returns == []
        assert timestamps == []

    def test_logs_candidate_id_on_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Silent-failure contract: a zero-contribution candidate must be
        # visible in logs with the candidate id so operators can diagnose.
        # (structlog renders to stdout rather than stdlib logging.)
        _extract_returns_from_account(pd.DataFrame(), candidate_id="cand-xyz")
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "cand-xyz" in combined
        assert "portfolio_candidate_empty_account" in combined


# ---------------------------------------------------------------------------
# _prepare_strategy_config
# ---------------------------------------------------------------------------


class TestPrepareStrategyConfig:
    def test_injects_instrument_id_and_bar_type(self) -> None:
        result = _prepare_strategy_config({}, ["AAPL.NASDAQ"])
        assert result["instrument_id"] == "AAPL.NASDAQ"
        assert result["bar_type"] == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"

    def test_preserves_explicit_values(self) -> None:
        result = _prepare_strategy_config(
            {"instrument_id": "BTC.BINANCE", "bar_type": "BTC-5-MINUTE"},
            ["ETH.BINANCE"],
        )
        # Caller's explicit values win over defaults.
        assert result["instrument_id"] == "BTC.BINANCE"
        assert result["bar_type"] == "BTC-5-MINUTE"

    def test_empty_instrument_ids_leaves_config_untouched(self) -> None:
        result = _prepare_strategy_config({"foo": 1}, [])
        assert result == {"foo": 1}

    def test_returns_copy_not_reference(self) -> None:
        src = {"foo": 1}
        result = _prepare_strategy_config(src, ["AAPL.NASDAQ"])
        result["mutated"] = True
        assert "mutated" not in src


# ---------------------------------------------------------------------------
# _raw_benchmark_symbol
# ---------------------------------------------------------------------------


class TestRawBenchmarkSymbol:
    def test_strips_uppercase_venue_suffix(self) -> None:
        assert _raw_benchmark_symbol("SPY.NASDAQ") == "SPY"
        assert _raw_benchmark_symbol("AAPL.XNAS") == "AAPL"
        assert _raw_benchmark_symbol("BRK.B.NYSE") == "BRK.B"

    def test_preserves_share_class(self) -> None:
        # Single-letter suffixes are share classes, not venues — must
        # NOT be stripped, otherwise a fallback could silently substitute
        # the parent ticker and compute alpha/beta against the wrong
        # asset if both are in the parquet store.
        assert _raw_benchmark_symbol("BRK.B") == "BRK.B"
        assert _raw_benchmark_symbol("RDS.A") == "RDS.A"

    def test_preserves_lowercase_and_mixed_case(self) -> None:
        # Only uppercase codes are treated as venues.
        assert _raw_benchmark_symbol("FOO.xyz") == "FOO.xyz"
        assert _raw_benchmark_symbol("FOO.Xyz") == "FOO.Xyz"

    def test_no_dot_returns_unchanged(self) -> None:
        assert _raw_benchmark_symbol("SPY") == "SPY"
