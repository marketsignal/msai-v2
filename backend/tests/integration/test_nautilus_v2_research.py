"""Native economic owning checks on tiny synthetic catalogs, not public E2E."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from nautilus_trader.model import Bar, BarType, Price, Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.instruments import resolve_instrument

_ROOT = Path(__file__).resolve().parents[3]


def _catalog(path: Path, prices=("100.00", "103.00")) -> Path:
    path.mkdir(parents=True)
    catalog = ParquetDataCatalog(str(path))
    instrument = resolve_instrument("AAPL", venue="NASDAQ")
    catalog.write_instruments([instrument])
    bar_type = BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL")
    bars = []
    for minute, price in enumerate(prices):
        ts = pd.Timestamp("2025-01-02T12:00:00Z").value + minute * 60_000_000_000
        p = Price.from_str(price)
        bars.append(Bar(bar_type, p, p, p, p, Quantity.from_int(100), ts, ts))
    catalog.write_bars(bars)
    return path


@pytest.mark.parametrize("fast", [1, 2])
def test_research_parameters_receive_engine_identity_and_execute_native(tmp_path, fast):
    params = {"fast_ema_period": fast, "slow_ema_period": 3, "trade_size": "1"}
    result = BacktestRunner().run(
        strategy_file=str(_ROOT / "strategies/example/ema_cross.py"),
        strategy_config=params,
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=_catalog(
            tmp_path / "native", ("100.00", "101.00", "102.00", "101.00", "100.00")
        ),
    )
    assert result.metrics["num_bars"] == 5
    assert result.metrics["num_fills"] == 2
    assert set(result.fills_df["instrument_id"].map(str)) == {"AAPL.NASDAQ"}
    assert "instrument_id" not in params and "bar_type" not in params


@pytest.mark.parametrize("manage_stop", ["false", 0])
def test_validated_boolean_reaches_actual_native_construction(tmp_path, manage_stop):
    params = {"manage_stop": manage_stop}
    result = BacktestRunner().run(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config=params,
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=_catalog(tmp_path / "native"),
    )
    assert result.metrics["num_bars"] == 2
    assert result.metrics["num_fills"] == 1
    assert params == {"manage_stop": manage_stop}


@pytest.mark.parametrize(
    "bar_type",
    [
        "AAPL.NASDAQ-5-MINUTE-LAST-EXTERNAL",
        "AAPL.NASDAQ-1-MINUTE-MID-EXTERNAL",
        "AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL",
        "AAPL.NASDAQ-5-MINUTE-MID-INTERNAL",
    ],
)
def test_native_mismatched_bar_subscription_refuses_result(tmp_path, bar_type):
    catalog = _catalog(tmp_path / "native")
    probe = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--subscription", str(catalog), bar_type],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(_ROOT / "backend/src"), str(_ROOT)))},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    assert result["ok"] is False, result
    assert "bar_type must match the backtest data" in result["error"]
    assert result["callbacks"] == 0


def test_native_run_filters_other_bar_types_in_same_catalog(tmp_path):
    catalog = _catalog(tmp_path / "native")
    other_type = BarType.from_str("AAPL.NASDAQ-1-MINUTE-MID-EXTERNAL")
    price = Price.from_str("999.00")
    ts = pd.Timestamp("2025-01-02T12:00:00Z").value
    ParquetDataCatalog(str(catalog)).write_bars(
        [Bar(other_type, price, price, price, price, Quantity.from_int(100), ts, ts)]
    )
    result = BacktestRunner().run(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={"manage_stop": False},
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
    )
    assert result.metrics["num_bars"] == 2
    assert result.metrics["num_fills"] == 1
    assert list(result.fills_df["last_px"].map(str)) == ["100.00"]


def test_common_runner_reconciles_native_tiny_reference(tmp_path):
    catalog = _catalog(tmp_path / "native")
    result = BacktestRunner().run(
        strategy_file=str(_ROOT / "strategies/example/cycle_buy_sell.py"),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "cycle_bars": 1,
            "quantity": 10,
            "manage_stop": False,
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
        initial_capital=10_000,
        commission_per_fill=1,
    )
    assert result.metrics["num_bars"] == 2
    assert result.metrics["num_fills"] == 2
    assert list(result.fills_df["last_px"].map(str)) == ["100.00", "103.00"]
    assert list(result.fills_df["last_qty"].map(str)) == ["10", "10"]
    assert list(result.fills_df["commission"].map(str)) == ["1.00 USD", "1.00 USD"]
    assert result.account_df.iloc[-1]["equity"] == pytest.approx(10_028)
    assert result.metrics["total_return"] == pytest.approx(0.0028)
    assert result.accounting["basis"] == "realized_account_balance"
    assert result.accounting["engine_version"] == "2.0.0rc6"
    assert result.accounting["leverage"] == 1
    assert result.fills_df["trade_id"].nunique() == 2


def test_open_position_does_not_substitute_marked_equity(tmp_path):
    catalog = _catalog(tmp_path / "native")
    result = BacktestRunner().run(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "manage_stop": False,
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
        initial_capital=10_000,
        commission_per_fill=1,
    )
    probe = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(catalog)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(_ROOT / "backend/src"), str(_ROOT)))},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    marked = json.loads(probe.stdout.strip().splitlines()[-1])
    assert result.metrics["num_fills"] == 1
    assert result.account_df.iloc[-1]["equity"] == pytest.approx(9999)
    assert result.metrics["total_return"] == pytest.approx(-0.0001)
    assert marked["equity"] == pytest.approx(10_002)
    assert marked["unrealized_pnl"] == pytest.approx(3)
    assert result.account_df.iloc[-1]["equity"] != marked["equity"]
    assert "LONG" in set(result.positions_df["side"].astype(str))


def _native_mark_probe(catalog):
    from nautilus_trader.backtest import BacktestNode

    from msai.services.nautilus.backtest_runner import (
        _build_backtest_run_config,
        _build_strategy_config,
        _RunPayload,
    )

    payload = _RunPayload(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "manage_stop": False,
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
        initial_capital=10_000,
        commission_per_fill=1,
    )
    cfg = _build_backtest_run_config(payload)
    node = BacktestNode([cfg])
    try:
        node.build()
        node.add_strategy_from_config(cfg.id, _build_strategy_config(payload))
        node.run()
        from nautilus_trader.model import Venue

        portfolio = node.get_engine_portfolio(cfg.id)
        venue = Venue("NASDAQ")
        print(
            json.dumps(
                {
                    "equity": float(next(iter(portfolio.equity(venue).values())).as_decimal()),
                    "unrealized_pnl": float(
                        next(iter(portfolio.unrealized_pnls(venue).values())).as_decimal()
                    ),
                }
            )
        )
    finally:
        node.dispose()


@pytest.mark.parametrize("callback", ["on_start", "on_bar", "on_stop", "on_dispose"])
def test_actual_native_callback_exception_refuses_result(tmp_path, callback):
    catalog = _catalog(tmp_path / "native")
    probe = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--callback", str(catalog), callback],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(_ROOT / "backend/src"), str(_ROOT)))},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    assert result["ok"] is False
    assert f"intentional {callback} failure" in result["error"]


def test_actual_intentionally_failing_strategy_refuses_result(tmp_path):
    with pytest.raises(RuntimeError, match="intentional failure for E2E test"):
        BacktestRunner().run(
            strategy_file=str(_ROOT / "strategies/intentionally_failing_strategy.py"),
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            },
            instrument_ids=["AAPL.NASDAQ"],
            start_date="2025-01-02",
            end_date="2025-01-02",
            catalog_path=_catalog(tmp_path / "native"),
        )


def test_actual_native_run_preserves_data_error_with_configured_raise_exception(tmp_path):
    catalog = _catalog(tmp_path / "native")
    probe = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--native-error", str(catalog)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(_ROOT / "backend/src"), str(_ROOT)))},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    assert result["raised"] is True
    assert "parquet" in result["error"].lower()


def _native_run_error_probe(catalog):
    from nautilus_trader.backtest import BacktestNode

    from msai.services.nautilus.backtest_runner import _build_backtest_run_config, _RunPayload

    payload = _RunPayload(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={},
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
    )
    config = _build_backtest_run_config(payload)
    assert config.raise_exception is True
    node = BacktestNode([config])
    try:
        node.build()
        # Keep the successfully built engine/instrument; fail actual data loading
        # during native run, using only this test's private catalog.
        files = list((Path(catalog) / "data" / "bars").rglob("*.parquet"))
        assert files
        for file in files:
            file.write_bytes(b"intentional invalid parquet fixture")
        try:
            node.run()
        except Exception as exc:
            print(json.dumps({"raised": True, "error": str(exc)}))
        else:
            print(json.dumps({"raised": False, "error": None}))
    finally:
        node.dispose()


def _native_callback_probe(catalog, callback):
    import pickle

    from strategies.example.smoke_market_order import SmokeMarketOrderStrategy

    from msai.services.nautilus.backtest_runner import _run_in_subprocess, _RunPayload

    def broken_callback(self, *args, **kwargs):
        raise RuntimeError(f"intentional {callback} failure")

    setattr(SmokeMarketOrderStrategy, callback, broken_callback)
    payload = _RunPayload(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "manage_stop": False,
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
        result_path=str(Path(catalog).parent / "failure.pkl"),
    )
    _run_in_subprocess(payload)
    with open(payload.result_path, "rb") as handle:
        result = pickle.load(handle)
    print(json.dumps({"ok": result["ok"], "error": result.get("error")}))


def _native_subscription_probe(catalog, bar_type):
    import pickle

    from strategies.example.smoke_market_order import SmokeMarketOrderStrategy

    from msai.services.nautilus.backtest_runner import _run_in_subprocess, _RunPayload

    callbacks = []
    original = SmokeMarketOrderStrategy.on_bar

    def record_bar(self, bar):
        callbacks.append(str(bar.bar_type))
        return original(self, bar)

    SmokeMarketOrderStrategy.on_bar = record_bar
    payload = _RunPayload(
        strategy_file=str(_ROOT / "strategies/example/smoke_market_order.py"),
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": bar_type,
            "manage_stop": False,
        },
        instrument_ids=["AAPL.NASDAQ"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        catalog_path=catalog,
        result_path=str(Path(catalog).parent / "subscription.pkl"),
    )
    try:
        _run_in_subprocess(payload)
    finally:
        SmokeMarketOrderStrategy.on_bar = original
    with open(payload.result_path, "rb") as handle:
        result = pickle.load(handle)
    print(
        json.dumps(
            {
                "ok": result["ok"],
                "error": result.get("error"),
                "callbacks": len(callbacks),
                "metrics": result.get("metrics"),
            }
        )
    )


if __name__ == "__main__":
    if sys.argv[1] == "--callback":
        _native_callback_probe(*sys.argv[2:])
    elif sys.argv[1] == "--subscription":
        _native_subscription_probe(*sys.argv[2:])
    elif sys.argv[1] == "--native-error":
        _native_run_error_probe(sys.argv[2])
    else:
        _native_mark_probe(sys.argv[1])
