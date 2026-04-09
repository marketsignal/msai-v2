import pickle
from pathlib import Path

import pandas as pd
import pytest

from msai.services.nautilus.backtest_runner import (
    _build_backtest_run_config,
    _build_backtest_venue_configs,
    _compact_account_report,
    _extract_metrics,
    _RunInput,
    _write_subprocess_result,
)


def test_build_backtest_run_config_uses_importable_strategy_paths() -> None:
    strategy_file = (
        Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"
    )
    payload = _RunInput(
        strategy_path=str(strategy_file),
        config={
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
            "trade_size": "1",
        },
        instruments=["AAPL.XNAS"],
        start_date="2024-01-01",
        end_date="2024-02-01",
        data_path="./data/parquet",
        result_path="/tmp/test-backtest-result.pkl",
    )

    run_config = _build_backtest_run_config(payload)
    strategy = run_config.engine.strategies[0]
    data = run_config.data[0]

    assert strategy.strategy_path.endswith(":EMACrossStrategy")
    assert strategy.config_path.endswith(":EMACrossConfig")
    assert data.instrument_ids == payload.instruments
    assert run_config.venues[0].name == "XNAS"


def test_build_backtest_run_config_deduplicates_venues() -> None:
    venues = _build_backtest_venue_configs(["AAPL.XNAS", "MSFT.XNAS", "CLM26.XNYM"])

    assert [venue.name for venue in venues] == ["XNAS", "XNYM"]


def test_compact_account_report_aggregates_intraday_rows_to_daily() -> None:
    frame = pd.DataFrame(
        {
            "equity_total": ["100.0", "101.0", "105.0"],
        },
        index=pd.to_datetime(
            [
                "2026-04-07T14:30:00Z",
                "2026-04-07T14:31:00Z",
                "2026-04-08T14:30:00Z",
            ],
            utc=True,
        ),
    )

    compact = _compact_account_report(frame)

    assert list(compact["timestamp"].dt.strftime("%Y-%m-%d")) == ["2026-04-07", "2026-04-08"]
    assert compact["equity"].tolist() == [101.0, 105.0]
    assert compact["returns"].iloc[0] == 0.0
    assert compact["returns"].iloc[1] == (105.0 / 101.0) - 1.0


def test_compact_account_report_deduplicates_same_timestamp_account_snapshots() -> None:
    frame = pd.DataFrame(
        {
            "account_id": ["EQUS-001", "EQUS-001", "EQUS-001"],
            "equity_total": ["100.0", "101.0", "105.0"],
        },
        index=pd.to_datetime(
            [
                "2026-04-07T14:30:00Z",
                "2026-04-07T14:30:00Z",
                "2026-04-08T14:30:00Z",
            ],
            utc=True,
        ),
    )

    compact = _compact_account_report(frame)

    assert compact["equity"].tolist() == [101.0, 105.0]


def test_write_subprocess_result_persists_pickled_payload(tmp_path: Path) -> None:
    result_path = tmp_path / "backtest-result.pkl"

    _write_subprocess_result(str(result_path), {"ok": True, "metrics": {"sharpe": 1.23}})

    assert result_path.exists()
    with result_path.open("rb") as handle:
        payload = pickle.load(handle)
    assert payload["ok"] is True
    assert payload["metrics"]["sharpe"] == 1.23


def test_extract_metrics_falls_back_to_account_returns_for_drawdown() -> None:
    account = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-04-07T00:00:00Z",
                    "2026-04-08T00:00:00Z",
                    "2026-04-09T00:00:00Z",
                ],
                utc=True,
            ),
            "returns": [0.0, -0.10, 0.02],
            "equity": [100.0, 90.0, 91.8],
        }
    )

    metrics = _extract_metrics(
        type("Result", (), {"stats_returns": {}, "stats_pnls": {}})(),
        pd.DataFrame([{"id": 1}]),
        account,
    )

    assert metrics["max_drawdown"] == pytest.approx(-0.1, abs=1e-9)
    assert metrics["total_return"] == pytest.approx(-0.082, abs=1e-9)
