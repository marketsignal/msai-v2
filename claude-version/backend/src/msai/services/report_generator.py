"""QuantStats report generation for backtests.

Generates HTML tearsheet reports from returns series data, providing
comprehensive visual analysis of backtest performance.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
from pandas.api.types import is_numeric_dtype

from msai.core.logging import get_logger

log = get_logger(__name__)


def _normalize_report_returns(series: pd.Series | None) -> pd.Series:
    """Normalise a returns series for QuantStats.

    QuantStats treats every row as one trading period and annualises with
    ``sqrt(252)``, so minute-bar input inflates Sharpe/Sortino/vol by
    ``sqrt(390)`` (~20x).  We group by UTC calendar date and compound each
    day's returns back to a single daily observation.  Already-daily input
    round-trips unchanged because ``(1 + r).prod()`` over a one-element
    group equals ``1 + r``.

    Ported from codex-version/backend/src/msai/services/report_generator.py.
    """
    if series is None:
        return pd.Series(dtype=float)

    normalized = pd.Series(series).copy()
    if normalized.empty:
        return pd.Series(dtype=float)

    normalized = pd.to_numeric(normalized, errors="coerce").dropna()
    if normalized.empty:
        return pd.Series(dtype=float)

    index = normalized.index
    if isinstance(index, pd.DatetimeIndex):
        timestamp_index: pd.DatetimeIndex | None = index
    elif is_numeric_dtype(index):
        timestamp_index = None
    else:
        parsed = pd.to_datetime(index, utc=True, errors="coerce")
        timestamp_index = parsed if isinstance(parsed, pd.DatetimeIndex) else None

    if isinstance(timestamp_index, pd.DatetimeIndex) and not timestamp_index.isna().all():
        normalized.index = timestamp_index
        normalized = normalized[~normalized.index.isna()]
        if normalized.empty:
            return pd.Series(dtype=float)
        normalized = ((1.0 + normalized).groupby(normalized.index.normalize()).prod() - 1.0).astype(
            float
        )

    return normalized.sort_index()


# Try to import quantstats; gracefully degrade if unavailable.
try:
    import quantstats as qs

    _HAS_QUANTSTATS = True
except ImportError:
    _HAS_QUANTSTATS = False


class ReportGenerator:
    """Generates QuantStats HTML tearsheet reports from backtest returns."""

    @property
    def has_quantstats(self) -> bool:
        """Return True if QuantStats is available."""
        return _HAS_QUANTSTATS

    def generate_tearsheet(
        self,
        returns: pd.Series,  # type: ignore[type-arg]
        benchmark: pd.Series | None = None,  # type: ignore[type-arg]
        title: str = "MSAI Backtest Report",
    ) -> str:
        """Generate a QuantStats HTML tearsheet from a returns series.

        Args:
            returns: Period returns series (daily or intraday).
            benchmark: Optional benchmark returns series for comparison.
            title: Title for the report.

        Returns:
            HTML string of the tearsheet report.

        Raises:
            RuntimeError: If QuantStats is not installed.
        """
        # Compound intraday bars into daily returns — QuantStats assumes
        # one row per trading period, so minute-bar input produces a
        # ~sqrt(390) Sharpe inflation without this step. Already-daily
        # series round-trip unchanged.
        returns = _normalize_report_returns(returns)
        normalized_benchmark = (
            _normalize_report_returns(benchmark) if benchmark is not None else None
        )

        if not _HAS_QUANTSTATS:
            return self._generate_fallback_report(returns, title)

        if returns.empty:
            return self._generate_empty_report(title)

        try:
            # QuantStats writes HTML to a file and returns None.
            # We write to a temp file and read the content back.
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as tmp:
                tmp_path = tmp.name

            qs.reports.html(
                returns,
                benchmark=normalized_benchmark,
                title=title,
                output=tmp_path,
                download_filename=tmp_path,
            )

            html = Path(tmp_path).read_text(encoding="utf-8")
            Path(tmp_path).unlink(missing_ok=True)
            return html
        except Exception as exc:
            log.warning(
                "quantstats_report_failed",
                error=str(exc),
                fallback="generating basic report",
            )
            return self._generate_fallback_report(returns, title)

    def save_report(self, html: str, backtest_id: str, data_root: str) -> str:
        """Save an HTML report to disk.

        Writes the report to ``{data_root}/reports/{backtest_id}.html``.

        Args:
            html: HTML content string.
            backtest_id: Unique identifier for the backtest.
            data_root: Root directory for data storage.

        Returns:
            Absolute path to the saved report file.
        """
        reports_dir = Path(data_root) / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        report_path = reports_dir / f"{backtest_id}.html"
        report_path.write_text(html, encoding="utf-8")

        log.info("report_saved", path=str(report_path), backtest_id=backtest_id)
        return str(report_path)

    def get_report_path(self, backtest_id: str, data_root: str) -> Path:
        """Return the expected path for a backtest report.

        Args:
            backtest_id: Unique identifier for the backtest.
            data_root: Root directory for data storage.

        Returns:
            Path object for the report file (may not exist yet).
        """
        return Path(data_root) / "reports" / f"{backtest_id}.html"

    def _generate_fallback_report(
        self,
        returns: pd.Series,  # type: ignore[type-arg]
        title: str,
    ) -> str:
        """Generate a basic HTML report when QuantStats is not available.

        Args:
            returns: Period returns series.
            title: Title for the report.

        Returns:
            Basic HTML string with summary statistics.
        """
        total_return = float((1 + returns).prod() - 1) if len(returns) > 0 else 0.0
        num_periods = len(returns)

        return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<h2>Summary</h2>
<ul>
<li>Total Return: {total_return:.4%}</li>
<li>Number of Periods: {num_periods}</li>
</ul>
<p><em>Install quantstats for full tearsheet reports.</em></p>
</body>
</html>"""

    def _generate_empty_report(self, title: str) -> str:
        """Generate a report placeholder when returns are empty.

        Args:
            title: Title for the report.

        Returns:
            HTML string indicating no data.
        """
        return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>No returns data available for this backtest.</p>
</body>
</html>"""
