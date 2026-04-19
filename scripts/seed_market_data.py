"""Generate synthetic OHLCV minute bar data for E2E testing.

Creates realistic-looking 1-minute bars for AAPL, MSFT, SPY covering
January 2025 (~20 trading days × 390 minutes = ~7,800 bars per symbol).

Writes Parquet files to: {data_root}/parquet/stocks/{SYMBOL}/2025/01.parquet

Usage:
    python scripts/seed_market_data.py data
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SYMBOLS = {
    "AAPL": {"start_price": 243.0, "volatility": 0.0008, "drift": 0.00001},
    "MSFT": {"start_price": 420.0, "volatility": 0.0007, "drift": 0.000008},
    "SPY": {"start_price": 595.0, "volatility": 0.0004, "drift": 0.000005},
}

TRADING_HOURS_START = 9 * 60 + 30  # 9:30 AM ET in minutes
TRADING_HOURS_END = 16 * 60  # 4:00 PM ET in minutes
MINUTES_PER_DAY = TRADING_HOURS_END - TRADING_HOURS_START  # 390


def generate_trading_days(year: int, month: int) -> list[datetime]:
    """Return trading days (weekdays) for a given month."""
    days = []
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    current = start
    while current < end:
        if current.weekday() < 5:  # Mon-Fri
            days.append(current)
        current += timedelta(days=1)
    return days


def generate_bars(symbol: str, params: dict, year: int = 2025, month: int = 1) -> pd.DataFrame:
    """Generate realistic 1-minute OHLCV bars using geometric Brownian motion."""
    rng = np.random.default_rng(hash(symbol) % (2**31))
    trading_days = generate_trading_days(year, month)

    timestamps = []
    opens = []
    highs = []
    lows = []
    closes = []
    volumes = []

    price = params["start_price"]
    vol = params["volatility"]
    drift = params["drift"]

    for day in trading_days:
        base_volume = rng.integers(50_000, 200_000)

        for minute_offset in range(MINUTES_PER_DAY):
            hour = (TRADING_HOURS_START + minute_offset) // 60
            minute = (TRADING_HOURS_START + minute_offset) % 60
            ts = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            timestamps.append(ts)

            # Geometric Brownian motion for price
            open_price = price
            returns = rng.normal(drift, vol, size=4)
            intra_prices = open_price * np.cumprod(1 + returns)

            close_price = float(intra_prices[-1])
            high_price = float(max(open_price, np.max(intra_prices)))
            low_price = float(min(open_price, np.min(intra_prices)))

            # Volume: higher at open/close, lower midday
            hour_factor = 1.0
            if minute_offset < 30 or minute_offset > 360:
                hour_factor = 2.5  # Opening/closing surge
            elif minute_offset < 60:
                hour_factor = 1.5
            bar_volume = int(base_volume * hour_factor * rng.uniform(0.5, 1.5))

            opens.append(round(open_price, 2))
            highs.append(round(high_price, 2))
            lows.append(round(low_price, 2))
            closes.append(round(close_price, 2))
            volumes.append(bar_volume)

            price = close_price

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, utc=True),
        "symbol": symbol,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


def write_parquet(df: pd.DataFrame, data_root: Path, asset_class: str, symbol: str) -> Path:
    """Write a DataFrame as a Parquet file in the MSAI directory structure."""
    ts = df["timestamp"].iloc[0]
    year = f"{ts.year:04d}"
    month = f"{ts.month:02d}"

    target = data_root / "parquet" / asset_class / symbol / year / f"{month}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, str(target), compression="zstd")

    return target


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_market_data.py <data_root>")
        print("  e.g. python scripts/seed_market_data.py data")
        sys.exit(1)

    data_root = Path(sys.argv[1])
    print(f"Seeding market data to {data_root}")

    for symbol, params in SYMBOLS.items():
        df = generate_bars(symbol, params)
        path = write_parquet(df, data_root, "stocks", symbol)
        print(f"  {symbol}: {len(df)} bars → {path}")

    print("Done!")


if __name__ == "__main__":
    main()
