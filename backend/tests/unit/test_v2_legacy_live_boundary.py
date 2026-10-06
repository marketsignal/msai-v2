"""RC6 legacy entry points refuse before touching unavailable V1 contracts."""

import importlib
from datetime import date

import pytest

from msai.services.nautilus.runtime_capabilities import UnsupportedRuntimeError


@pytest.mark.parametrize(
    "module",
    [
        "msai.services.nautilus.data_freshness_actor",
        "msai.services.symbology_shim_actor",
    ],
)
def test_legacy_actor_import_refuses_actionably(module: str) -> None:
    with pytest.raises(UnsupportedRuntimeError, match="research-only runtime"):
        importlib.import_module(module)


@pytest.mark.parametrize(
    ("builder", "kwargs"),
    [
        ("build_redis_database_config", {}),
        ("build_per_account_strategy_configs", {"strategy_members": [], "deployment_slug": "test"}),
        (
            "build_live_trading_node_config",
            {
                "deployment_slug": "test",
                "strategy_path": "unused:Strategy",
                "strategy_config_path": "unused:Config",
                "strategy_config": {},
                "paper_symbols": [],
                "ib_settings": None,
            },
        ),
        (
            "build_portfolio_trading_node_config",
            {
                "deployment_slug": "test",
                "strategy_members": [],
                "ib_settings": None,
            },
        ),
        (
            "build_per_account_trading_node_config",
            {
                "account_id": "",
                "ib_login_key": "",
                "ibg_client_id": 1,
                "native_instrument_ids": [],
                "venue_dataset_map": {},
                "canonical_to_native_bar_types": {},
                "databento_api_key": "",
                "ib_host": "127.0.0.1",
                "ib_port": 65534,
            },
        ),
    ],
)
def test_legacy_config_builders_refuse_before_validation_or_native_import(
    builder: str,
    kwargs: dict[str, object],
) -> None:
    config = importlib.import_module("msai.services.nautilus.live_node_config")
    with pytest.raises(UnsupportedRuntimeError, match="research-only runtime"):
        getattr(config, builder)(**kwargs)


def test_native_independent_helpers_remain_usable() -> None:
    config = importlib.import_module("msai.services.nautilus.live_node_config")
    from msai.services.nautilus.live_instrument_bootstrap import current_quarterly_expiry

    assert config._has_us_equity_venue(["BRK.B.NYSE"])
    assert config._rewrite_bar_type_to_ibkr("AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL") == (
        "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL"
    )
    assert current_quarterly_expiry(date(2026, 6, 19)) == "202609"
