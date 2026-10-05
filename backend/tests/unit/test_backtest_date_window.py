"""Inclusive UTC windows exercised by a real, process-isolated BacktestNode."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_STAMPS = (
    "2024-12-02T15:00:00Z",
    "2024-12-03T15:00:00Z",
    "2024-12-03T23:59:59.999999999Z",
    "2024-12-04T00:00:00Z",
    "2024-12-04T15:00:00Z",
    "2024-12-05T15:00:00Z",
)


def _probe(start: str, end: str) -> dict[str, Any]:
    # Every engine gets a fresh interpreter: Nautilus owns global Rust state.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), start, end],
        env={**os.environ, "PYTHONPATH": str(_ROOT / "backend/src")},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2024-12-02", "2024-12-03", _STAMPS[:3]),
        ("2024-12-03", "2024-12-03", _STAMPS[1:3]),
        ("2024-12-04", "2024-12-04", _STAMPS[3:5]),
        ("2024-12-05", "2024-12-05", _STAMPS[5:]),
        ("2024-12-06", "2024-12-06", ()),
    ],
)
def test_real_engine_consumes_inclusive_calendar_days(
    start: str, end: str, expected: tuple[str, ...]
) -> None:
    observed = _probe(start, end)
    expected_ns = [pd.Timestamp(stamp).value for stamp in expected]
    assert observed["consumed_ns"] == expected_ns
    assert observed["iterations"] == len(expected_ns)
    assert observed["metrics"]["num_bars"] == len(expected_ns)
    start_ns = pd.Timestamp(start, tz="UTC").value
    end_ns = (pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).value - 1
    assert observed["data_bounds"] == observed["run_bounds"] == [start_ns, end_ns]
    assert pd.Timestamp(end, tz="UTC").value + pd.Timedelta(days=1).value not in expected_ns


def test_adjacent_train_test_and_purge_windows_do_not_overlap() -> None:
    train = _probe("2024-12-03", "2024-12-03")
    adjacent_test = _probe("2024-12-04", "2024-12-04")
    purged_test = _probe("2024-12-05", "2024-12-05")
    assert train["consumed_ns"] == [pd.Timestamp(stamp).value for stamp in _STAMPS[1:3]]
    assert adjacent_test["consumed_ns"] == [pd.Timestamp(stamp).value for stamp in _STAMPS[3:5]]
    assert purged_test["consumed_ns"] == [pd.Timestamp(_STAMPS[5]).value]
    assert set(train["consumed_ns"]).isdisjoint(adjacent_test["consumed_ns"])
    assert set(train["consumed_ns"]).isdisjoint(purged_test["consumed_ns"])
    assert set(adjacent_test["consumed_ns"]).isdisjoint(purged_test["consumed_ns"])


def _run_probe(start: str, end: str) -> None:
    import msgspec
    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.data import Bar, BarType
    from nautilus_trader.model.objects import Price, Quantity
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    from msai.services.nautilus.backtest_runner import (
        _build_backtest_run_config,
        _extract_metrics,
        _RunPayload,
    )

    bar_type = BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    with tempfile.TemporaryDirectory(prefix="msai-date-window-test-") as temporary:
        catalog = ParquetDataCatalog(temporary)
        catalog.write_data([TestInstrumentProvider.equity(symbol="AAPL", venue="NASDAQ")])
        price = Price.from_str("100.00")
        catalog.write_data(
            [
                Bar(
                    bar_type,
                    price,
                    price,
                    price,
                    price,
                    Quantity.from_int(100),
                    # Event time precedes init time: the next-midnight bar's
                    # ts_event is inside the prior day, but must be excluded.
                    pd.Timestamp(stamp).value - 1,
                    pd.Timestamp(stamp).value,
                )
                for stamp in _STAMPS
            ]
        )
        payload = _RunPayload(
            strategy_file=str(_ROOT / "strategies/example/ema_cross.py"),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": str(bar_type),
                "fast_ema_period": 2,
                "slow_ema_period": 3,
                "trade_size": "1",
            },
            instrument_ids=["AAPL.NASDAQ"],
            start_date=start,
            end_date=end,
            catalog_path=temporary,
        )
        config = _build_backtest_run_config(payload)
        config = msgspec.structs.replace(
            config,
            engine=msgspec.structs.replace(
                config.engine, logging=LoggingConfig(bypass_logging=True)
            ),
        )
        node = BacktestNode(configs=[config])
        try:
            results = node.run()
            engine = node.get_engine(config.id)
            consumed = sorted(int(bar.ts_init) for bar in engine.cache.bars(bar_type))
            data = config.data[0]
            print(
                json.dumps(
                    {
                        "consumed_ns": consumed,
                        "iterations": int(results[0].iterations),
                        "metrics": _extract_metrics(results[0], pd.DataFrame()),
                        "data_bounds": [data.start_time_nanos, data.end_time_nanos],
                        "run_bounds": [
                            pd.Timestamp(config.start).value,
                            pd.Timestamp(config.end).value,
                        ],
                    },
                    sort_keys=True,
                )
            )
        finally:
            node.dispose()


if __name__ == "__main__":
    _run_probe(*sys.argv[1:])
