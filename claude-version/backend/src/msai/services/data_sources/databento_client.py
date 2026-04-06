"""Databento API client for fetching futures OHLCV bar data.

Uses the Databento Python SDK to retrieve historical minute bars for
futures contracts.  Returns normalized DataFrames compatible with the
ParquetStore write format.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from msai.core.logging import get_logger

log = get_logger(__name__)


class DatabentoClient:
    """Client for the Databento Historical API (futures minute bars)."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def fetch_futures_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        dataset: str = "GLBX.MDP3",
        stype: str = "continuous",
    ) -> pd.DataFrame:
        """Fetch futures minute bars from Databento.

        Uses the ``databento`` Python SDK's ``Historical`` client to request
        OHLCV-1m bars.  The SDK call is synchronous, so it is run within the
        async context (the SDK handles I/O internally).

        Args:
            symbol: Futures symbol (e.g. ``"ES.FUT"``, ``"NQ.FUT"``).
            start: Start date as ``"YYYY-MM-DD"``.
            end: End date as ``"YYYY-MM-DD"``.
            dataset: Databento dataset identifier.
            stype: Symbol type (``"continuous"`` for front-month roll).

        Returns:
            DataFrame with columns: ``symbol``, ``timestamp``, ``open``,
            ``high``, ``low``, ``close``, ``volume``.
            Returns an empty DataFrame on error or no data.
        """
        try:
            import databento as db
        except ImportError as exc:
            log.error("databento_import_error", error=str(exc))
            return _empty_bars_df()

        try:
            client = db.Historical(key=self.api_key)
            data = client.timeseries.get_range(
                dataset=dataset,
                symbols=[symbol],
                stype_in=stype,
                schema="ohlcv-1m",
                start=start,
                end=end,
            )
            df = data.to_df()
        except Exception as exc:  # noqa: BLE001 — Databento SDK may raise varied errors
            log.error("databento_fetch_error", symbol=symbol, error=str(exc))
            return _empty_bars_df()

        if df.empty:
            return _empty_bars_df()

        return _normalize_databento_bars(df, symbol)


def _normalize_databento_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Convert a Databento OHLCV DataFrame to the canonical bar schema.

    Databento's ``ohlcv-1m`` schema includes ``ts_event`` as the timestamp
    and ``open``, ``high``, ``low``, ``close``, ``volume`` columns.
    """
    result = pd.DataFrame()
    result["symbol"] = [symbol] * len(df)

    # ts_event is the canonical event timestamp in Databento data.
    if "ts_event" in df.columns:
        result["timestamp"] = pd.to_datetime(df["ts_event"], utc=True)
    elif df.index.name == "ts_event":
        result["timestamp"] = pd.to_datetime(df.index, utc=True)
    else:
        # Fallback: use the DataFrame index.
        result["timestamp"] = pd.to_datetime(df.index, utc=True)

    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            result[col] = df[col].values

    return result[["symbol", "timestamp", "open", "high", "low", "close", "volume"]].reset_index(
        drop=True
    )


def _empty_bars_df() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical OHLCV schema."""
    return pd.DataFrame(
        columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"]
    )
