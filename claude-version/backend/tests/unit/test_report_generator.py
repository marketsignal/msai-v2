"""Unit tests for msai.services.report_generator module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

from msai.services.report_generator import ReportGenerator, _normalize_report_returns

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_returns(n: int = 100) -> pd.Series:  # type: ignore[type-arg]
    """Generate a sample returns series with random-ish data."""
    rng = np.random.default_rng(seed=42)
    returns = rng.normal(0.0005, 0.02, size=n)
    dates = pd.bdate_range("2024-01-02", periods=n)
    return pd.Series(returns, index=dates, name="returns")


def _empty_returns() -> pd.Series:  # type: ignore[type-arg]
    """Generate an empty returns series."""
    return pd.Series(dtype=float, name="returns")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenerateTearsheet:
    """Tests for ReportGenerator.generate_tearsheet."""

    def test_generate_tearsheet_returns_html(self) -> None:
        """Generate tearsheet from sample returns and verify it is HTML."""
        # Arrange
        generator = ReportGenerator()
        returns = _sample_returns(50)

        # Act
        html = generator.generate_tearsheet(returns, title="Test Report")

        # Assert
        assert isinstance(html, str)
        assert len(html) > 0
        assert "<html" in html.lower() or "<!doctype" in html.lower()

    def test_generate_tearsheet_with_empty_returns(self) -> None:
        """Generate tearsheet with empty returns produces valid HTML."""
        # Arrange
        generator = ReportGenerator()
        returns = _empty_returns()

        # Act
        html = generator.generate_tearsheet(returns, title="Empty Report")

        # Assert
        assert isinstance(html, str)
        assert "html" in html.lower()

    def test_generate_tearsheet_contains_title(self) -> None:
        """Generated HTML contains the provided title."""
        # Arrange
        generator = ReportGenerator()
        returns = _sample_returns(50)

        # Act
        html = generator.generate_tearsheet(returns, title="My Custom Title")

        # Assert
        assert "My Custom Title" in html


class TestSaveReport:
    """Tests for ReportGenerator.save_report."""

    def test_save_report_creates_file(self, tmp_path: Path) -> None:
        """Save report and verify the file exists on disk."""
        # Arrange
        generator = ReportGenerator()
        html = "<html><body><h1>Test</h1></body></html>"
        backtest_id = "test-backtest-123"

        # Act
        path = generator.save_report(html, backtest_id, str(tmp_path))

        # Assert
        from pathlib import Path as P

        report_file = P(path)
        assert report_file.exists()
        assert report_file.name == f"{backtest_id}.html"
        assert report_file.read_text() == html

    def test_save_report_creates_reports_directory(self, tmp_path: Path) -> None:
        """Save report creates the reports/ subdirectory if it does not exist."""
        # Arrange
        generator = ReportGenerator()
        html = "<html><body>report</body></html>"
        data_root = str(tmp_path / "nested" / "data")

        # Act
        path = generator.save_report(html, "bt-001", data_root)

        # Assert
        from pathlib import Path as P

        assert P(path).exists()
        assert "reports" in path

    def test_save_report_returns_absolute_path(self, tmp_path: Path) -> None:
        """Returned path string is the absolute file path."""
        # Arrange
        generator = ReportGenerator()
        html = "<html></html>"

        # Act
        path = generator.save_report(html, "bt-abs", str(tmp_path))

        # Assert
        from pathlib import Path as P

        assert P(path).is_absolute()


class TestGetReportPath:
    """Tests for ReportGenerator.get_report_path."""

    def test_get_report_path_returns_expected_path(self, tmp_path: Path) -> None:
        """get_report_path returns {data_root}/reports/{backtest_id}.html."""
        # Arrange
        generator = ReportGenerator()

        # Act
        path = generator.get_report_path("bt-42", str(tmp_path))

        # Assert
        assert path.name == "bt-42.html"
        assert path.parent.name == "reports"


# ---------------------------------------------------------------------------
# Intraday normalization — the point of this fix
# ---------------------------------------------------------------------------
#
# QuantStats assumes one observation per trading period. Feeding it 390
# minute bars per day without compounding makes Sharpe/Sortino/vol explode
# (~20x) because annualisation sees 390 * 252 "periods" per year instead
# of 252. The normaliser groups by calendar date (UTC) and compounds each
# day's returns back to a single daily observation.


class TestNormalizeReportReturns:
    def test_intraday_returns_compound_to_one_row_per_day(self) -> None:
        # 2 days x 390 minute bars, each bar +0.001.
        timestamps = pd.date_range(
            start="2024-01-02 09:30", periods=390, freq="1min", tz="UTC"
        ).append(pd.date_range(start="2024-01-03 09:30", periods=390, freq="1min", tz="UTC"))
        returns = pd.Series([0.001] * len(timestamps), index=timestamps)

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == 2
        # (1 + 0.001) ** 390 - 1 ~= 0.4712
        expected_daily = (1.001) ** 390 - 1
        assert normalized.iloc[0] == pytest.approx(expected_daily, rel=1e-9)
        assert normalized.iloc[1] == pytest.approx(expected_daily, rel=1e-9)

    def test_already_daily_returns_round_trip_unchanged(self) -> None:
        # Daily bars must not be re-compounded — groupby(day).prod() on a
        # single-per-day series must reproduce the input exactly.
        dates = pd.date_range("2024-01-02", periods=10, freq="B", tz="UTC")
        values = [0.01, -0.02, 0.015, 0.0, -0.005, 0.02, 0.01, -0.01, 0.0, 0.008]
        returns = pd.Series(values, index=dates)

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == len(returns)
        pd.testing.assert_series_equal(
            normalized.astype(float).reset_index(drop=True),
            returns.astype(float).reset_index(drop=True),
            check_names=False,
        )

    def test_empty_series_returns_empty(self) -> None:
        assert _normalize_report_returns(pd.Series(dtype=float)).empty

    def test_none_returns_empty(self) -> None:
        assert _normalize_report_returns(None).empty

    def test_non_numeric_values_are_dropped(self) -> None:
        # Strings must be coerced and dropped; surviving numeric rows still normalise.
        idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"], utc=True)
        returns = pd.Series(["0.01", "bad", "0.02"], index=idx)

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == 2
        assert normalized.iloc[0] == pytest.approx(0.01)
        assert normalized.iloc[1] == pytest.approx(0.02)

    def test_non_datetime_index_passes_through(self) -> None:
        # RangeIndex (numeric) — QuantStats will still reject it later,
        # but the normaliser must not raise or try to groupby a non-datetime.
        returns = pd.Series([0.01, 0.02, 0.03])

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == 3

    def test_unsorted_timestamps_are_sorted(self) -> None:
        idx = pd.to_datetime(["2024-01-04", "2024-01-02", "2024-01-03"], utc=True)
        returns = pd.Series([0.03, 0.01, 0.02], index=idx)

        normalized = _normalize_report_returns(returns)

        assert list(normalized.values) == pytest.approx([0.01, 0.02, 0.03])

    def test_intraday_bars_crossing_utc_midnight_group_by_utc_day(self) -> None:
        # Bars straddle UTC midnight: 23:00-23:59 on day 1, 00:00-00:59 on day 2.
        # Must land in two daily buckets when keyed by UTC calendar date.
        idx = pd.date_range("2024-01-02 23:00", periods=60, freq="1min", tz="UTC").append(
            pd.date_range("2024-01-03 00:00", periods=60, freq="1min", tz="UTC")
        )
        returns = pd.Series([0.0001] * len(idx), index=idx)

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == 2
        assert normalized.index[0].date().isoformat() == "2024-01-02"
        assert normalized.index[1].date().isoformat() == "2024-01-03"

    def test_tz_naive_datetime_index_is_handled(self) -> None:
        # Tz-naive DatetimeIndex takes the short-circuit branch (no utc re-parse).
        # Still must groupby-day and compound without raising.
        idx = pd.date_range("2024-01-02 09:30", periods=390, freq="1min").append(
            pd.date_range("2024-01-03 09:30", periods=390, freq="1min")
        )
        assert idx.tz is None  # sanity
        returns = pd.Series([0.0001] * len(idx), index=idx)

        normalized = _normalize_report_returns(returns)

        assert len(normalized) == 2

    def test_intraday_sharpe_matches_daily_sharpe_after_normalize(self) -> None:
        # Regression guard on the economic symptom: minute-bar returns
        # that compound to a known daily series must yield the SAME Sharpe
        # as the equivalent daily series. If anyone ever removes the
        # groupby step, this fails immediately.
        daily_idx = pd.date_range("2024-01-02", periods=10, freq="B", tz="UTC")
        daily_returns = pd.Series(
            [0.01, -0.02, 0.015, 0.0, -0.005, 0.02, 0.01, -0.01, 0.0, 0.008],
            index=daily_idx,
        )

        # Build equivalent intraday: split each day's return into 390 equal
        # multiplicative bar returns so (1+r_bar)**390 == 1+r_daily.
        intraday_rows: list[tuple[pd.Timestamp, float]] = []
        for day, r in daily_returns.items():
            bar_r = (1.0 + r) ** (1.0 / 390) - 1.0
            day_ts = pd.Timestamp(day).tz_convert("UTC")
            for minute in range(390):
                intraday_rows.append((day_ts + pd.Timedelta(minutes=minute), bar_r))
        intraday = pd.Series(
            [r for _, r in intraday_rows],
            index=pd.DatetimeIndex([ts for ts, _ in intraday_rows]),
        )

        normalized = _normalize_report_returns(intraday)

        assert len(normalized) == len(daily_returns)
        pd.testing.assert_series_equal(
            normalized.astype(float).reset_index(drop=True),
            daily_returns.astype(float).reset_index(drop=True),
            rtol=1e-9,
            check_names=False,
        )


class TestTearsheetUsesNormalizedReturns:
    def test_generate_tearsheet_does_not_crash_on_minute_bars(self) -> None:
        # Before the fix, QuantStats would either crash or produce
        # nonsense annualized stats. After the fix, intraday input
        # compounds to daily internally and a valid HTML doc is returned.
        generator = ReportGenerator()
        timestamps = pd.date_range(start="2024-01-02 09:30", periods=5 * 390, freq="1min", tz="UTC")
        rng = np.random.default_rng(seed=7)
        returns = pd.Series(rng.normal(0.0, 0.0005, size=len(timestamps)), index=timestamps)

        html = generator.generate_tearsheet(returns, title="Intraday Test")

        assert isinstance(html, str)
        assert "<html" in html.lower() or "<!doctype" in html.lower()
