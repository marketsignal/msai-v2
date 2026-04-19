"""Polygon.io REST API client for fetching OHLCV bar data.

Uses httpx async client with rate limiting to fetch aggregate bars from
the Polygon.io v2 API.  Returns normalized DataFrames compatible with
the ParquetStore write format.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from msai.core.logging import get_logger

log = get_logger(__name__)

# Polygon free tier: 5 requests/minute.  Paid tiers are higher but we
# default to a conservative limit that can be overridden.
_DEFAULT_RATE_LIMIT_DELAY = 0.25  # seconds between requests


class PolygonClient:
    """Async client for the Polygon.io Aggregates (Bars) API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.polygon.io",
        rate_limit_delay: float = _DEFAULT_RATE_LIMIT_DELAY,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.rate_limit_delay = rate_limit_delay

    async def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        timespan: str = "minute",
        multiplier: int = 1,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars from Polygon REST API.

        Uses the ``/v2/aggs/ticker/{ticker}/range/`` endpoint with pagination
        to handle large date ranges.

        Args:
            symbol: Ticker symbol (e.g. ``"AAPL"``).
            start: Start date as ``"YYYY-MM-DD"``.
            end: End date as ``"YYYY-MM-DD"``.
            timespan: Bar timespan (``"minute"``, ``"hour"``, ``"day"``).
            multiplier: Number of timespans per bar (e.g. ``1`` for 1-minute).

        Returns:
            DataFrame with columns: ``symbol``, ``timestamp``, ``open``,
            ``high``, ``low``, ``close``, ``volume``.
            Returns an empty DataFrame if no results.
        """
        all_results: list[dict[str, Any]] = []
        url = (
            f"{self.base_url}/v2/aggs/ticker/{symbol}/range"
            f"/{multiplier}/{timespan}/{start}/{end}"
        )
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "adjusted": "true",
            "sort": "asc",
            "limit": 50_000,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            while url:
                await asyncio.sleep(self.rate_limit_delay)
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                results: list[dict[str, Any]] = data.get("results", [])
                all_results.extend(results)

                log.info(
                    "polygon_page_fetched",
                    symbol=symbol,
                    results_count=len(results),
                    total_so_far=len(all_results),
                )

                # Polygon pagination: next_url contains the full URL for the
                # next page (no need to re-add params except apiKey).
                next_url: str | None = data.get("next_url")
                if next_url:
                    url = next_url
                    params = {"apiKey": self.api_key}
                else:
                    url = ""  # type: ignore[assignment]

        if not all_results:
            return _empty_bars_df()

        return _normalize_polygon_bars(all_results, symbol)


def _normalize_polygon_bars(results: list[dict[str, Any]], symbol: str) -> pd.DataFrame:
    """Convert Polygon API results to a normalized OHLCV DataFrame.

    Polygon returns bars with keys: ``t`` (timestamp ms), ``o``, ``h``, ``l``,
    ``c``, ``v``.  We rename and convert to the canonical schema.
    """
    df = pd.DataFrame(results)
    df = df.rename(
        columns={
            "t": "timestamp",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
        }
    )
    # Polygon timestamps are Unix milliseconds.
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["symbol"] = symbol

    return df[["symbol", "timestamp", "open", "high", "low", "close", "volume"]].reset_index(
        drop=True
    )


def _empty_bars_df() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical OHLCV schema."""
    return pd.DataFrame(
        columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"]
    )
