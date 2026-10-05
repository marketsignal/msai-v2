"""Unit tests for ``msai.services.nautilus.backtest_runner``.

These tests exercise only the in-process config-builder pieces of the
runner.  They deliberately do NOT spin up an actual ``BacktestNode``
subprocess because that requires a populated Nautilus catalog plus a
sizeable chunk of CPU time -- the end-to-end path is covered by the
integration smoke test in Docker.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest

from msai.services.nautilus.backtest_runner import (
    _build_backtest_run_config,
    _extract_metrics,
    _extract_venues_from_instrument_ids,
    _RunPayload,
    _zero_metrics,
)

if TYPE_CHECKING:
    from nautilus_trader.backtest.config import BacktestRunConfig

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
        assert run_config.start == data.start_time == 1704067200000000000
        assert run_config.end == data.end_time == 1706831999999999999

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
        result = _extract_venues_from_instrument_ids(["AAPL.NASDAQ", "ESM5.CME", "MSFT.NASDAQ"])
        assert result == ["NASDAQ", "CME"]

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
        """A backtest spanning ``["AAPL.NASDAQ", "ESM5.CME"]`` gets
        TWO ``BacktestVenueConfig`` entries — one per unique
        venue — so Nautilus's engine can route orders to the
        correct simulated venue."""
        payload = _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ", "ESM5.CME"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            catalog_path="./data/nautilus",
        )
        run_config = _build_backtest_run_config(payload)

        venue_names = sorted(v.name for v in run_config.venues)
        assert venue_names == ["CME", "NASDAQ"]


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
        assert "num_bars" not in metrics


def _window_config(start: str, end: str) -> BacktestRunConfig:
    return _build_backtest_run_config(
        _RunPayload(
            strategy_file=str(_STRATEGY_FILE),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ"],
            start_date=start,
            end_date=end,
            catalog_path="./data/nautilus",
        )
    )


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        ("2024-02-29", "2024-02-29", 1709164800000000000, 1709251199999999999),
        ("2024-12-31", "2025-01-01", 1735603200000000000, 1735775999999999999),
        ("1970-01-01", "1970-01-01", 0, 86399999999999),
        (
            "2024-12-03T12:00:00.000000001Z",
            "2024-12-03T15:00:00.000000002Z",
            1733227200000000001,
            1733238000000000002,
        ),
        (
            "2024-12-03T07:00:00-05:00",
            "2024-12-03",
            1733227200000000000,
            1733270399999999999,
        ),
        (
            "2024-12-03",
            "2024-12-03T07:00:00-05:00",
            1733184000000000000,
            1733227200000000000,
        ),
    ],
)
def test_execution_bounds_preserve_utc_dates_and_exact_instants(
    start: str, end: str, expected_start: int, expected_end: int
) -> None:
    config = _window_config(start, end)
    assert config.start == config.data[0].start_time == expected_start
    assert config.end == config.data[0].end_time == expected_end


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2024-12-03", "2024-12-02", "End date must be on or after start date"),
        ("2024-12-03T13:00:00Z", "2024-12-03T12:00:00Z", "End date must be"),
        ("2024-02-30", "2024-03-01", "Invalid backtest start"),
        ("20241203", "2024-12-04", "Invalid backtest start"),
        ("2024-W49-2", "2024-12-04", "Invalid backtest start"),
        ("2024-12-03", "not-a-date", "Invalid backtest end"),
        ("1969-12-31", "1970-01-01", "supported nanosecond range"),
        ("2262-04-11", "2262-04-11", "supported nanosecond range"),
        ("9999-12-31", "9999-12-31", "supported nanosecond range"),
    ],
)
def test_invalid_or_unrepresentable_window_fails_before_engine(
    start: str, end: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _window_config(start, end)


@pytest.mark.parametrize("count", [0, 3])
def test_metrics_expose_actual_native_bar_count_including_zero(count: int) -> None:
    metrics = _extract_metrics(SimpleNamespace(iterations=count), pd.DataFrame())
    assert metrics["num_bars"] == count
    assert isinstance(metrics["num_bars"], int)


def test_metrics_without_native_count_do_not_fabricate_zero() -> None:
    assert "num_bars" not in _extract_metrics(SimpleNamespace(), pd.DataFrame())
