from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from msai.core.config import settings
from msai.services.analytics_math import build_series_from_returns, dataframe_to_series_payload


class BacktestAnalyticsNotFoundError(FileNotFoundError):
    """Raised when backtest analytics are unavailable."""


class BacktestAnalyticsService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.backtest_analytics_root

    def save(
        self,
        *,
        backtest_id: str,
        account_df: pd.DataFrame,
        metrics: dict[str, Any],
        report_path: Path | None = None,
    ) -> Path:
        payload = self.build_payload(
            backtest_id=backtest_id,
            account_df=account_df,
            metrics=metrics,
            report_path=report_path,
        )
        target = self._path(backtest_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return target

    def load(self, backtest_id: str) -> dict[str, Any]:
        path = self._path(backtest_id)
        if not path.exists():
            raise BacktestAnalyticsNotFoundError(f"Backtest analytics not found: {backtest_id}")
        return json.loads(path.read_text())

    def build_payload(
        self,
        *,
        backtest_id: str,
        account_df: pd.DataFrame,
        metrics: dict[str, Any],
        report_path: Path | None = None,
    ) -> dict[str, Any]:
        frame = _build_chart_frame(account_df)
        return {
            "id": backtest_id,
            "metrics": json.loads(json.dumps(metrics)),
            "series": dataframe_to_series_payload(frame),
            "report_url": f"/api/v1/backtests/{backtest_id}/report" if report_path is not None else None,
        }

    def _path(self, backtest_id: str) -> Path:
        return self.root / f"{backtest_id}.json"


def _build_chart_frame(account_df: pd.DataFrame) -> pd.DataFrame:
    if account_df.empty:
        return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])

    frame = account_df.copy()
    if isinstance(frame.index, pd.DatetimeIndex):
        frame = frame.reset_index().rename(columns={frame.index.name or "index": "timestamp"})
    timestamp_col = _first_present(frame.columns, ("timestamp", "ts_last", "ts_event", "ts_init", "datetime", "date"))
    if timestamp_col is None:
        return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])

    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col], utc=True, errors="coerce")
    frame = frame.dropna(subset=[timestamp_col]).sort_values(timestamp_col)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])

    equity_col = _first_present(
        frame.columns,
        ("equity", "equity_total", "balance_total", "total", "balance", "net_liquidation"),
    )
    returns_col = _first_present(frame.columns, ("returns", "return", "pnl_pct", "pnl_percent"))

    if equity_col is not None:
        frame[equity_col] = pd.to_numeric(frame[equity_col], errors="coerce")
        frame = frame.dropna(subset=[equity_col])
        if frame.empty:
            return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])
        account_id_col = _first_present(frame.columns, ("account_id",))
        if account_id_col is not None:
            deduped = (
                frame.groupby([timestamp_col, account_id_col], as_index=False)[equity_col]
                .last()
            )
            grouped = deduped.groupby(timestamp_col, as_index=True)[equity_col].sum().sort_index()
        else:
            grouped = frame.groupby(timestamp_col, as_index=True)[equity_col].last().sort_index()
        returns = grouped.pct_change().fillna(0.0)
        chart_frame = build_series_from_returns(returns, base_value=float(grouped.iloc[0] or 1.0))
        chart_frame["equity"] = grouped.values
        chart_frame["drawdown"] = (grouped / grouped.cummax() - 1.0).values
        return chart_frame

    if returns_col is not None:
        grouped_returns = (
            frame.groupby(timestamp_col, as_index=True)[returns_col]
            .mean()
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(0.0)
        )
        return build_series_from_returns(grouped_returns, base_value=1.0)

    return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])


def _first_present(columns: pd.Index, names: tuple[str, ...]) -> str | None:
    lowered = {str(column).lower(): str(column) for column in columns}
    for name in names:
        match = lowered.get(name.lower())
        if match is not None:
            return match
    return None
