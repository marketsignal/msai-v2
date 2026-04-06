"""Unit tests for msai.services.report_generator module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

from msai.services.report_generator import ReportGenerator

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
