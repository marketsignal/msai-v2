from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import quantstats as qs

from msai.core.config import settings


class ReportGenerator:
    def __init__(self, reports_root: Path | None = None) -> None:
        self.reports_root = reports_root or settings.reports_root

    def generate_tearsheet(self, returns_series: pd.Series, benchmark: pd.Series | None = None) -> str:
        if returns_series.empty:
            return "<html><body><h1>QuantStats</h1><p>No returns data available.</p></body></html>"

        html = qs.reports.html(returns_series, benchmark=benchmark, output=None)
        if isinstance(html, str):
            return html

        with NamedTemporaryFile(suffix=".html", delete=False) as temp:
            temp_path = Path(temp.name)
        qs.reports.html(returns_series, benchmark=benchmark, output=str(temp_path))
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
