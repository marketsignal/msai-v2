import pytest

from msai.services.nautilus.strategy_config import (
    prepare_backtest_strategy_config,
    prepare_live_strategy_config,
)


def test_prepare_live_strategy_config_injects_defaults_from_first_instrument() -> None:
    config = prepare_live_strategy_config(
        {"fast_ema_period": 10},
        ["AAPL.XNAS"],
    )

    assert config["instrument_id"] == "AAPL.XNAS"
    assert config["bar_type"] == "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"
    assert config["fast_ema_period"] == 10


def test_prepare_live_strategy_config_uses_explicit_instrument_id() -> None:
    config = prepare_live_strategy_config(
        {"instrument_id": "MSFT.XNAS", "trade_size": "2"},
        ["AAPL.XNAS", "MSFT.XNAS"],
    )

    assert config["instrument_id"] == "MSFT.XNAS"
    assert config["bar_type"] == "MSFT.XNAS-1-MINUTE-LAST-EXTERNAL"


def test_prepare_live_strategy_config_rejects_non_canonical_live_ids() -> None:
    with pytest.raises(ValueError, match="venue-qualified Nautilus instrument ID"):
        prepare_live_strategy_config({}, ["AAPL"])


def test_prepare_live_strategy_config_rejects_instrument_not_in_selection() -> None:
    with pytest.raises(ValueError, match="must match one of the selected instruments"):
        prepare_live_strategy_config({"instrument_id": "MSFT.XNAS"}, ["AAPL.XNAS"])


def test_prepare_backtest_strategy_config_rewrites_instrument_and_bar_prefix() -> None:
    config = prepare_backtest_strategy_config(
        {
            "instrument_id": "AAPL.XNAS",
            "bar_type": "AAPL.XNAS-5-MINUTE-LAST-EXTERNAL",
            "trade_size": "1",
        },
        ["AAPL.XNAS"],
    )

    assert config["instrument_id"] == "AAPL.XNAS"
    assert config["bar_type"] == "AAPL.XNAS-5-MINUTE-LAST-EXTERNAL"
