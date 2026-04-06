from __future__ import annotations

import pandas as pd

from msai.core.config import settings


class DatabentoClient:
    def __init__(self, api_key: str | None = None, dataset: str = "GLBX.MDP3") -> None:
        self.api_key = api_key or settings.databento_api_key
        self.dataset = dataset

    async def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        schema: str = "ohlcv-1m",
    ) -> pd.DataFrame:
        if not self.api_key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")

        try:
            import databento as db

            client = db.Historical(key=self.api_key)
            data = client.timeseries.get_range(
                dataset=self.dataset,
                schema=schema,
                symbols=[symbol],
                start=start,
                end=end,
            )
            df = data.to_df().reset_index()
            if "ts_event" in df.columns:
                df = df.rename(columns={"ts_event": "timestamp"})
            if "volume" not in df.columns and "size" in df.columns:
                df = df.rename(columns={"size": "volume"})
            required = ["timestamp", "open", "high", "low", "close", "volume"]
            missing = [col for col in required if col not in df.columns]
            if missing:
                raise RuntimeError(f"Databento response missing columns: {missing}")
            return df[required]
        except Exception:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
