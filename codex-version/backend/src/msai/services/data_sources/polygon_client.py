from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import pandas as pd

from msai.core.config import settings


class PolygonClient:
    def __init__(self, api_key: str | None = None, max_requests_per_sec: int = 5) -> None:
        self.api_key = api_key or settings.polygon_api_key
        self._semaphore = asyncio.Semaphore(max_requests_per_sec)
        self._base_url = "https://api.polygon.io"

    async def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        timespan: str = "minute",
    ) -> pd.DataFrame:
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY is not configured")

        path = f"/v2/aggs/ticker/{symbol}/range/1/{timespan}/{start}/{end}"
        params: dict[str, str | int] = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self.api_key,
        }

        async with self._semaphore:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()

        rows = response.json().get("results", [])
        if not rows:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        frame = pd.DataFrame(
            {
                "timestamp": [datetime.utcfromtimestamp(item["t"] / 1000) for item in rows],
                "open": [item["o"] for item in rows],
                "high": [item["h"] for item in rows],
                "low": [item["l"] for item in rows],
                "close": [item["c"] for item in rows],
                "volume": [item.get("v", 0) for item in rows],
            }
        )
        frame["symbol"] = symbol
        return frame

    async def fetch_options_chain(self, underlying: str, start: str, end: str) -> pd.DataFrame:
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY is not configured")

        path = "/v3/reference/options/contracts"
        params: dict[str, str | int] = {
            "underlying_ticker": underlying,
            "expiration_date.gte": start,
            "expiration_date.lte": end,
            "limit": 1000,
            "apiKey": self.api_key,
        }
        async with self._semaphore:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()

        rows = response.json().get("results", [])
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)
