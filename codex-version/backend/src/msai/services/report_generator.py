from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import quantstats as qs
from pandas.api.types import is_numeric_dtype

from msai.core.config import settings


class ReportGenerator:
    def __init__(self, reports_root: Path | None = None) -> None:
        self.reports_root = reports_root or settings.reports_root

    def generate_tearsheet(self, returns_series: pd.Series, benchmark: pd.Series | None = None) -> str:
        normalized_returns = _normalize_report_returns(returns_series)
        normalized_benchmark = _normalize_report_returns(benchmark) if benchmark is not None else None

        if normalized_returns.empty:
            return "<html><body><h1>QuantStats</h1><p>No returns data available.</p></body></html>"

        html = qs.reports.html(normalized_returns, benchmark=normalized_benchmark, output=None)
        if isinstance(html, str):
            return html

        with NamedTemporaryFile(suffix=".html", delete=False) as temp:
            temp_path = Path(temp.name)
        qs.reports.html(normalized_returns, benchmark=normalized_benchmark, output=str(temp_path))
        content = temp_path.read_text()
        temp_path.unlink(missing_ok=True)
        return content

    def save_report(self, html: str, backtest_id: str) -> Path:
        self.reports_root.mkdir(parents=True, exist_ok=True)
        target = self.reports_root / f"{backtest_id}.html"
        target.write_text(html)
        return target

    def get_report_path(self, backtest_id: str) -> Path:
        return self.reports_root / f"{backtest_id}.html"


def _normalize_report_returns(series: pd.Series | None) -> pd.Series:
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
        timestamp_index = index
    elif is_numeric_dtype(index):
        timestamp_index = None
    else:
        timestamp_index = pd.to_datetime(index, utc=True, errors="coerce")
    if isinstance(timestamp_index, pd.DatetimeIndex) and not timestamp_index.isna().all():
        normalized.index = timestamp_index
        normalized = normalized[~normalized.index.isna()]
        if normalized.empty:
            return pd.Series(dtype=float)
        # QuantStats is built around period returns. Intraday series are compounded
        # into daily returns so report generation stays tractable and meaningful.
        normalized = ((1.0 + normalized).groupby(normalized.index.normalize()).prod() - 1.0).astype(float)

    return normalized.sort_index()
