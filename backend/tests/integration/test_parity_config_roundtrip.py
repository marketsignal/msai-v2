"""Research config parity at the native RC6 constructor and JSON boundary.

RC6 has no V1 StrategyConfig.parse or msgspec encoding-hook API. The common
declared-field validator constructs real native configs from persisted user JSON;
API and worker preparation must agree on engine-owned identities. These offline
checks do not certify unsupported RC6 live deployment or require external services.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import BarType, InstrumentId
from nautilus_trader.trading import StrategyConfig

from msai.services.nautilus.schema_hooks import validate_strategy_config

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_EMA_JSON = {
    "instrument_id": "AAPL.NASDAQ",
    "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
    "fast_ema_period": 10,
    "slow_ema_period": 30,
    "trade_size": "1",
}


def test_research_config_constructs_real_native_strategy() -> None:
    from strategies.example.config import EMACrossConfig
    from strategies.example.ema_cross import EMACrossStrategy

    decoded = validate_strategy_config(EMACrossConfig, json.loads(json.dumps(_EMA_JSON)))
    assert isinstance(decoded, StrategyConfig)
    assert isinstance(decoded.instrument_id, InstrumentId)
    assert isinstance(decoded.bar_type, BarType)
    assert decoded.trade_size == Decimal("1")
    assert decoded.fast_ema_period == 10
    assert decoded.slow_ema_period == 30
    assert str(decoded.instrument_id) == "AAPL.NASDAQ"
    assert EMACrossStrategy(decoded)._halt_gate_armed is False


def test_ema_rejects_undeclared_live_manage_stop_field() -> None:
    from strategies.example.config import EMACrossConfig

    # RC6 native constructors accept extra kwargs; the declared-field boundary
    # must refuse an unsupported user field rather than skip/xfail this contract.
    with pytest.raises(ValueError, match="manage_stop"):
        validate_strategy_config(EMACrossConfig, _EMA_JSON | {"manage_stop": True})


def test_smoke_native_constructor_preserves_stop_and_tag_fields() -> None:
    from strategies.example.smoke_market_order import SmokeMarketOrderConfig

    payload = {
        "instrument_id": "AAPL.NASDAQ",
        "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        "manage_stop": True,
        "order_id_tag": "abcd1234abcd1234",
    }
    decoded = validate_strategy_config(SmokeMarketOrderConfig, payload)
    direct = SmokeMarketOrderConfig(**payload)
    for config in (decoded, direct):
        assert isinstance(config, StrategyConfig)
        assert isinstance(config.instrument_id, InstrumentId)
        assert isinstance(config.bar_type, BarType)
        assert config.manage_stop is True
        assert config.order_id_tag == "abcd1234abcd1234"


def test_smoke_rejects_native_strategy_separator_in_order_tag() -> None:
    from strategies.example.smoke_market_order import SmokeMarketOrderConfig

    payload = {
        "instrument_id": "AAPL.NASDAQ",
        "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        "order_id_tag": "0-abcd1234abcd1234",
    }
    # This old V1 tag is invalid in the actual RC6 native constructor.
    with pytest.raises(ValueError, match="strategy ID separator"):
        validate_strategy_config(SmokeMarketOrderConfig, payload)
    with pytest.raises(ValueError, match="strategy ID separator"):
        SmokeMarketOrderConfig(**payload)


def test_json_round_trip_preserves_native_user_field_types() -> None:
    from strategies.example.config import EMACrossConfig

    once = validate_strategy_config(EMACrossConfig, json.loads(json.dumps(_EMA_JSON)))
    encoded_fields = {
        "instrument_id": str(once.instrument_id),
        "bar_type": str(once.bar_type),
        "fast_ema_period": once.fast_ema_period,
        "slow_ema_period": once.slow_ema_period,
        "trade_size": str(once.trade_size),
    }
    assert encoded_fields == _EMA_JSON
    twice = validate_strategy_config(EMACrossConfig, json.loads(json.dumps(encoded_fields)))
    assert isinstance(twice.instrument_id, InstrumentId)
    assert isinstance(twice.bar_type, BarType)
    assert isinstance(twice.trade_size, Decimal)
    assert once.instrument_id == twice.instrument_id
    assert once.bar_type == twice.bar_type
    assert once.trade_size == twice.trade_size
    assert once.fast_ema_period == twice.fast_ema_period
    assert once.slow_ema_period == twice.slow_ema_period


def test_api_and_worker_inject_identical_configs_for_omitted_defaults() -> None:
    """Persisted research config and the worker's engine identities must agree."""
    from msai.api.backtests import _prepare_and_validate_backtest_config
    from msai.workers.backtest_job import _prepare_strategy_config

    strategy_file = _REPO_ROOT / "strategies" / "example" / "ema_cross.py"
    canonical = ["AAPL.NASDAQ"]
    user_config = {"fast_ema_period": 5, "slow_ema_period": 20}
    api_result = _prepare_and_validate_backtest_config(
        dict(user_config),
        strategy_file_path=str(strategy_file),
        config_class_name="EMACrossConfig",
        canonical_instruments=canonical,
    )
    worker_result = _prepare_strategy_config(dict(user_config), canonical)
    assert api_result == worker_result

    user_config_with_override = {
        "instrument_id": "MSFT.NASDAQ",
        "bar_type": "MSFT.NASDAQ-5-MINUTE-LAST-EXTERNAL",
        "fast_ema_period": 3,
    }
    api_override = _prepare_and_validate_backtest_config(
        dict(user_config_with_override),
        strategy_file_path=str(strategy_file),
        config_class_name="EMACrossConfig",
        canonical_instruments=canonical,
    )
    worker_override = _prepare_strategy_config(dict(user_config_with_override), canonical)
    assert api_override == worker_override
    # Preparation preserves caller aggregation while canonicalizing the prefix;
    # this does not certify unsupported five-minute execution in this candidate.
    assert api_override["instrument_id"] == "AAPL.NASDAQ"
    assert api_override["bar_type"] == "AAPL.NASDAQ-5-MINUTE-LAST-EXTERNAL"
