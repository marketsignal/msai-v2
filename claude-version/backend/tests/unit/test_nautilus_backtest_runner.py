"""Unit tests for ``msai.services.nautilus.backtest_runner``.

These tests exercise only the in-process config-builder pieces of the
runner.  They deliberately do NOT spin up an actual ``BacktestNode``
subprocess because that requires a populated Nautilus catalog plus a
sizeable chunk of CPU time -- the end-to-end path is covered by the
integration smoke test in Docker.
"""

from __future__ import annotations

from pathlib import Path

from msai.services.nautilus.backtest_runner import (
    _RunPayload,
    _build_backtest_run_config,
    _zero_metrics,
)

_STRATEGY_FILE = (
    Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"
)


class TestBuildBacktestRunConfig:
    """Tests for the private ``_build_backtest_run_config`` helper."""

    def test_config_wires_importable_strategy_paths(self) -> None:
        """The built config carries the resolved strategy and config paths."""
        # Arrange
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.SIM",
                "bar_type": "AAPL.SIM-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 30,
                "trade_size": "1",
            },
            instrument_ids=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-02-01",
            catalog_path="./data/nautilus",
        )

        # Act
        run_config = _build_backtest_run_config(payload)

        # Assert
        strategy = run_config.engine.strategies[0]
        data = run_config.data[0]

        assert strategy.strategy_path.endswith(":EMACrossStrategy")
        assert strategy.config_path.endswith(":EMACrossConfig")
        assert data.instrument_ids == payload.instrument_ids
        assert data.catalog_path == payload.catalog_path
        assert run_config.start == payload.start_date
        assert run_config.end == payload.end_date

    def test_venue_is_sim(self) -> None:
        """The backtest config declares the SIM venue with a starting balance."""
        # Arrange
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.SIM",
                "bar_type": "AAPL.SIM-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )

        # Act
        run_config = _build_backtest_run_config(payload)

        # Assert
        assert len(run_config.venues) == 1
        venue = run_config.venues[0]
        assert venue.name == "SIM"
        assert venue.starting_balances[0].endswith("USD")


class TestZeroMetrics:
    """Tests for the ``_zero_metrics`` helper."""

    def test_zero_metrics_contains_all_expected_keys(self) -> None:
        """All standard metric keys are present and zeroed out."""
        metrics = _zero_metrics()

        assert metrics["num_trades"] == 0
        assert metrics["sharpe_ratio"] == 0.0
        assert metrics["sortino_ratio"] == 0.0
        assert metrics["max_drawdown"] == 0.0
        assert metrics["total_return"] == 0.0
        assert metrics["win_rate"] == 0.0
