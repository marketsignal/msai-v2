"""Native user-config contract: discovery and useful refusal before dispatch."""

from decimal import Decimal
from pathlib import Path

import pytest

from msai.services.nautilus.schema_hooks import (
    ConfigSchemaStatus,
    build_user_schema,
    validate_strategy_config,
)
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths
from msai.services.strategy_registry import discover_strategies

ROOT = Path(__file__).resolve().parents[3] / "strategies"
VALID = {"instrument_id": "AAPL.NASDAQ", "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"}


def test_actual_strategy_discovery_and_user_schema():
    discovered = discover_strategies(ROOT)
    assert {s.name for s in discovered} == {
        "example.ema_cross",
        "example.cycle_buy_sell",
        "example.cycle_buy_sell_b",
        "example.smoke_market_order",
        "intentionally_failing_strategy",
    }
    assert all(s.config_schema_status == ConfigSchemaStatus.READY for s in discovered)
    assert all(s.governance_status == "passed" for s in discovered)
    ema = next(s for s in discovered if s.name == "example.ema_cross")
    assert ema.config_class_name == "EMACrossConfig"
    assert ema.config_schema_status == ConfigSchemaStatus.READY
    assert ema.default_config == {"fast_ema_period": 10, "slow_ema_period": 30, "trade_size": "1"}
    assert set(ema.config_schema["required"]) == {"instrument_id", "bar_type"}
    assert resolve_importable_strategy_paths(
        str(ROOT / "example/ema_cross.py")
    ).config_path.endswith(":EMACrossConfig")


def test_typed_defaults_and_native_strategy_construction():
    from nautilus_trader.model import BarType, InstrumentId
    from strategies.example.config import EMACrossConfig
    from strategies.example.ema_cross import EMACrossStrategy

    config = validate_strategy_config(EMACrossConfig, VALID)
    assert isinstance(config.instrument_id, InstrumentId)
    assert isinstance(config.bar_type, BarType)
    assert config.trade_size == Decimal("1")
    strategy = EMACrossStrategy(config)
    assert strategy._halt_gate_armed is False
    assert strategy.fast_ema.period == 10
    # The backtest-style instance has no wired live latch or quantitative caps.
    # A never-populated live halt cache must not block its opening-order gate.
    from types import SimpleNamespace

    assert strategy._halt_cache is None
    assert strategy._risk_limits is None
    assert strategy._gate_allows(SimpleNamespace(is_reduce_only=False, tags=[]))


@pytest.mark.parametrize(
    "change, field",
    [
        ({"fast_ema_period": 0}, "fast_ema_period"),
        ({"slow_ema_period": -1}, "slow_ema_period"),
        ({"fast_ema_period": "ten"}, "fast_ema_period"),
        ({"trade_size": "0"}, "trade_size"),
        ({"trade_size": "-1"}, "trade_size"),
        ({"trade_size": "NaN"}, "trade_size"),
        ({"instrument_id": "AAPL"}, "instrument_id"),
        ({"bar_type": "bad"}, "bar_type"),
        ({"fast_ema_peroid": 2}, "fast_ema_peroid"),
    ],
)
def test_bad_user_fields_are_rejected(change, field):
    from strategies.example.config import EMACrossConfig

    with pytest.raises(ValueError, match=field):
        validate_strategy_config(EMACrossConfig, VALID | change)


def test_required_fields_are_rejected():
    from strategies.example.config import EMACrossConfig

    with pytest.raises(ValueError, match="instrument_id"):
        validate_strategy_config(EMACrossConfig, {})


@pytest.mark.parametrize(
    "module_name, config_name, strategy_name",
    [
        ("cycle_buy_sell", "CycleBuySellConfig", "CycleBuySellStrategy"),
        ("cycle_buy_sell_b", "CycleBuySellBConfig", "CycleBuySellBStrategy"),
        ("smoke_market_order", "SmokeMarketOrderConfig", "SmokeMarketOrderStrategy"),
    ],
)
def test_other_tracked_native_configs(module_name, config_name, strategy_name):
    import importlib

    module = importlib.import_module(f"strategies.example.{module_name}")
    config_cls = getattr(module, config_name)
    schema, defaults, status = build_user_schema(config_cls)
    assert status is ConfigSchemaStatus.READY
    assert schema["properties"]["instrument_id"]["x-format"] == "instrument-id"
    assert defaults["manage_stop"] is True
    config = validate_strategy_config(config_cls, VALID)
    getattr(module, strategy_name)(config)
    direct = config_cls(**VALID)
    assert direct.manage_stop is True
    assert str(direct.bar_type) == VALID["bar_type"]
    assert getattr(module, strategy_name)(direct)._halt_gate_armed is False
    assert config_cls(**(VALID | {"manage_stop": False})).manage_stop is False
    assert validate_strategy_config(config_cls, VALID | {"manage_stop": False}).manage_stop is False
    if "cycle" in module_name:
        with pytest.raises(ValueError, match="quantity"):
            validate_strategy_config(config_cls, VALID | {"quantity": -1})
        with pytest.raises(ValueError, match="cycle_bars"):
            validate_strategy_config(config_cls, VALID | {"cycle_bars": 0})


def test_api_validation_rejects_unknown_field_and_malformed_bar():
    from msai.api.backtests import (
        StrategyConfigValidationError,
        _prepare_and_validate_backtest_config,
    )

    kwargs = {
        "strategy_file_path": str(ROOT / "example/ema_cross.py"),
        "config_class_name": "EMACrossConfig",
        "canonical_instruments": ["AAPL.NASDAQ"],
    }
    for config, field in [({"trade_szie": 1}, "trade_szie"), ({"bar_type": "garbage"}, "bar_type")]:
        with pytest.raises(StrategyConfigValidationError) as error:
            _prepare_and_validate_backtest_config(config, **kwargs)
        assert error.value.field == field
    prepared = _prepare_and_validate_backtest_config({}, **kwargs)
    assert prepared["instrument_id"] == "AAPL.NASDAQ"
    assert prepared["bar_type"] == VALID["bar_type"]


def test_actual_native_ema_callbacks_check_positions_and_execute(tmp_path):
    """Run actual EMA callbacks through the native node, not a mocked portfolio."""
    import pandas as pd
    from nautilus_trader.model import Bar, BarType, Price, Quantity
    from nautilus_trader.persistence import ParquetDataCatalog

    from msai.services.nautilus.backtest_runner import BacktestRunner
    from msai.services.nautilus.instruments import resolve_instrument

    catalog_path = tmp_path / "native-ema"
    catalog_path.mkdir()
    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_instruments([resolve_instrument("AAPL", venue="NASDAQ")])
    bar_type = BarType.from_str(VALID["bar_type"])
    bars = []
    for minute, value in enumerate(("100.00", "101.00", "102.00", "101.00", "100.00")):
        timestamp = pd.Timestamp("2025-01-02T12:00:00Z").value + minute * 60_000_000_000
        price = Price.from_str(value)
        bars.append(
            Bar(bar_type, price, price, price, price, Quantity.from_int(100), timestamp, timestamp)
        )
    catalog.write_bars(bars)
    result = BacktestRunner().run(
        strategy_file=str(ROOT / "example/ema_cross.py"),
        strategy_config=VALID | {"fast_ema_period": 2, "slow_ema_period": 3},
        instrument_ids=["AAPL.NASDAQ"], start_date="2025-01-02", end_date="2025-01-02",
        catalog_path=catalog_path,
    )
    assert result.metrics["num_bars"] == 5
    assert result.metrics["num_fills"] == 2
    assert len(result.fills_df) == 2
