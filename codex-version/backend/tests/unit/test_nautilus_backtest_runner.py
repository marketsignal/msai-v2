from pathlib import Path

from msai.services.nautilus.backtest_runner import _build_backtest_run_config, _RunInput


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
    )

    run_config = _build_backtest_run_config(payload)
    strategy = run_config.engine.strategies[0]
    data = run_config.data[0]

    assert strategy.strategy_path.endswith(":EMACrossStrategy")
    assert strategy.config_path.endswith(":EMACrossConfig")
    assert data.instrument_ids == payload.instruments
