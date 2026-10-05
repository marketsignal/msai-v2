#!/usr/bin/env python3
"""Reproduce M15 with the pinned engine; requires backend's Python dependencies.

Run once with 'primary' and once with 'control' before the repair. The emitted
JSON reports consumed engine data. This diagnostic is not a regression gate;
the owning regression must require the repaired calendar-date behavior.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "backend/src"))
sys.path.insert(0, str(root))

import msgspec  # noqa: E402
import pandas as pd  # noqa: E402
from nautilus_trader.backtest.node import BacktestNode  # noqa: E402
from nautilus_trader.config import LoggingConfig  # noqa: E402
from nautilus_trader.model.data import Bar, BarType  # noqa: E402
from nautilus_trader.model.objects import Price, Quantity  # noqa: E402
from nautilus_trader.persistence.catalog import ParquetDataCatalog  # noqa: E402
from nautilus_trader.test_kit.providers import TestInstrumentProvider  # noqa: E402

from msai.services.nautilus.backtest_runner import (  # noqa: E402
    _build_backtest_run_config,
    _RunPayload,
)


def main() -> None:
    if sys.argv[1:] not in (["primary"], ["control"]):
        raise SystemExit("Use primary or control")
    case = sys.argv[1]
    bar_type = BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    stamps = [
        "2024-12-02T15:00:00Z",
        "2024-12-03T15:00:00Z",
        "2024-12-03T23:59:59.999999999Z",
        "2024-12-04T00:00:00Z",
        "2024-12-04T15:00:00Z",
    ]
    with tempfile.TemporaryDirectory(prefix="msai-window-repro-") as temporary:
        catalog = ParquetDataCatalog(temporary)
        catalog.write_data([TestInstrumentProvider.equity(symbol="AAPL", venue="NASDAQ")])
        price = Price.from_str("100.00")
        catalog.write_data([
            Bar(bar_type, price, price, price, price, Quantity.from_int(100),
                pd.Timestamp(stamp).value, pd.Timestamp(stamp).value)
            for stamp in stamps
        ])
        payload = _RunPayload(
            strategy_file=str(root / "strategies/example/ema_cross.py"),
            strategy_config={"instrument_id": "AAPL.NASDAQ", "bar_type": str(bar_type),
                             "fast_ema_period": 2, "slow_ema_period": 3, "trade_size": "1"},
            instrument_ids=["AAPL.NASDAQ"], start_date="2024-12-02",
            end_date=("2024-12-03" if case == "primary" else "2024-12-03T23:59:59.999999999Z"),
            catalog_path=temporary,
        )
        config = _build_backtest_run_config(payload)
        config = msgspec.structs.replace(config, engine=msgspec.structs.replace(
            config.engine, logging=LoggingConfig(bypass_logging=True)))
        node = BacktestNode(configs=[config])
        try:
            results = node.run()
            engine = node.get_engine(config.id)
            consumed = sorted(int(bar.ts_init) for bar in engine.cache.bars(bar_type))
            report = {"case": case, "iterations": int(results[0].iterations),
                      "consumed_ns": consumed,
                      "dates": sorted({
                          pd.Timestamp(ts, tz="UTC").date().isoformat() for ts in consumed
                      }),
                      "next_midnight_included": (
                          pd.Timestamp("2024-12-04T00:00:00Z").value in consumed
                      )}
            print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        finally:
            node.dispose()


if __name__ == "__main__":
    main()
