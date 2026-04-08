"""Unit tests for ``msai.services.nautilus.backtest_runner``.

These tests exercise only the in-process config-builder pieces of the
runner.  They deliberately do NOT spin up an actual ``BacktestNode``
subprocess because that requires a populated Nautilus catalog plus a
sizeable chunk of CPU time -- the end-to-end path is covered by the
integration smoke test in Docker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from msai.services.nautilus.backtest_runner import (
    _RunPayload,
    _build_backtest_run_config,
    _extract_venues_from_instrument_ids,
    _zero_metrics,
)

_STRATEGY_FILE = Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"


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


class TestExtractVenuesFromInstrumentIds:
    """Phase 2 task 2.9: derive per-backtest venue list from the
    canonical instrument IDs in the payload so the runner builds
    one ``BacktestVenueConfig`` per unique venue."""

    def test_single_venue_equity(self) -> None:
        assert _extract_venues_from_instrument_ids(["AAPL.NASDAQ"]) == ["NASDAQ"]

    def test_duplicates_collapse(self) -> None:
        assert _extract_venues_from_instrument_ids(["AAPL.NASDAQ", "MSFT.NASDAQ"]) == ["NASDAQ"]

    def test_multi_venue_preserves_first_seen_order(self) -> None:
        """A mixed backtest (equity + futures) produces both venues
        in first-seen order — deterministic so tests can assert on
        the resulting config list."""
        result = _extract_venues_from_instrument_ids(["AAPL.NASDAQ", "ESM5.XCME", "MSFT.NASDAQ"])
        assert result == ["NASDAQ", "XCME"]

    def test_option_venue_is_last_dot_component(self) -> None:
        """Option ids have internal spaces + multiple dots
        (``"C AAPL 20260515 150.SMART"``). The venue is everything
        after the FINAL ``.``."""
        assert _extract_venues_from_instrument_ids(["C AAPL 20260515 150.SMART"]) == ["SMART"]

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            _extract_venues_from_instrument_ids([])

    def test_missing_venue_suffix_raises(self) -> None:
        """A bare ticker without the ``.VENUE`` suffix is a migration
        bug — we reject at config-build time rather than letting
        Nautilus crash mid-backtest."""
        with pytest.raises(ValueError, match="venue suffix"):
            _extract_venues_from_instrument_ids(["AAPL"])


class TestMultiVenueBuildConfig:
    def test_multi_venue_produces_one_config_per_venue(self) -> None:
        """A backtest spanning ``["AAPL.NASDAQ", "ESM5.XCME"]`` gets
        TWO ``BacktestVenueConfig`` entries — one per unique
        venue — so Nautilus's engine can route orders to the
        correct simulated venue."""
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ", "ESM5.XCME"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )
        run_config = _build_backtest_run_config(payload)

        venue_names = sorted(v.name for v in run_config.venues)
        assert venue_names == ["NASDAQ", "XCME"]


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
