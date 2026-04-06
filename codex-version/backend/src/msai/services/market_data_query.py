from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from msai.core.config import settings


class MarketDataQuery:
    def __init__(self, data_root: Path | None = None) -> None:
        self.data_root = data_root or settings.data_root
        self.parquet_root = self.data_root / "parquet"

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1m") -> dict:
        files = list(self.parquet_root.glob(f"*/{symbol}/**/*.parquet"))
        if not files:
            return {"symbol": symbol, "bars": []}

        con = duckdb.connect(":memory:")
        try:
            df = con.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM read_parquet(?)
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp
                """,
                [str(self.parquet_root / f"*/{symbol}/**/*.parquet"), start, end],
            ).fetch_df()
        finally:
            con.close()

        if df.empty:
            return {"symbol": symbol, "bars": []}

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if interval != "1m":
            rule = {"5m": "5min", "1h": "1h", "1d": "1d"}.get(interval, "1min")
            df = (
                df.set_index("timestamp")
                .resample(rule)
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna()
                .reset_index()
            )

        bars = [
            {
                "timestamp": ts.isoformat(),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
            for ts, open_, high, low, close, volume in df.itertuples(index=False)
        ]
        return {"symbol": symbol, "bars": bars}

    def get_symbols(self) -> dict[str, list[str]]:
        symbols: dict[str, list[str]] = {}
        if not self.parquet_root.exists():
            return symbols

        for asset_dir in sorted(self.parquet_root.iterdir()):
            if not asset_dir.is_dir():
                continue
            symbols[asset_dir.name] = sorted(path.name for path in asset_dir.iterdir() if path.is_dir())
        return symbols

    def get_storage_stats(self) -> dict:
        stats: dict[str, dict[str, int]] = {}
        if not self.parquet_root.exists():
            return stats

        for asset_dir in sorted(self.parquet_root.iterdir()):
            if not asset_dir.is_dir():
                continue
            files = list(asset_dir.rglob("*.parquet"))
            total_bytes = sum(path.stat().st_size for path in files)
            stats[asset_dir.name] = {"file_count": len(files), "bytes": total_bytes}
        return stats
