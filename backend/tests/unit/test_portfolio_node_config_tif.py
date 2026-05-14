"""TIF=DAY injection in build_portfolio_trading_node_config (Bug #2,
live-deploy-safety-trio).

Mirrors test_live_node_config.py's single-strategy TIF tests for the
multi-strategy portfolio path. Both injection sites must behave
identically so a strategy moved from a single-strategy deployment into
a portfolio doesn't suddenly lose its TIF override.
"""

from __future__ import annotations

from nautilus_trader.model.enums import TimeInForce

from datetime import date
from typing import Any
from uuid import uuid4

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_portfolio_trading_node_config,
)
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
)
from msai.services.nautilus.trading_node_subprocess import StrategyMemberPayload


def _synth_resolved(instruments: list[str]) -> tuple[ResolvedInstrument, ...]:
    return tuple(
        ResolvedInstrument(
            canonical_id=sym if "." in sym else f"{sym}.NASDAQ",
            asset_class=AssetClass.EQUITY
            if "." in sym and not sym.endswith(".CME")
            else AssetClass.FUTURES,
            contract_spec={"symbol": sym.split(".")[0]},
            effective_window=(date(2020, 1, 1), None),
        )
        for sym in instruments
    )


def _make_member(
    *,
    instruments: list[str],
    strategy_config: dict[str, Any] | None = None,
    strategy_id_full: str = "EMACrossStrategy-0-abcd1234abcd1234",
) -> StrategyMemberPayload:
    return StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config=strategy_config if strategy_config is not None else {},
        strategy_id_full=strategy_id_full,
        instruments=instruments,
        resolved_instruments=_synth_resolved(instruments),
    )


_IB = IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567")
_SLUG = "abcd1234abcd1234"


def test_portfolio_tif_day_injected_when_member_targets_us_equity() -> None:
    """Bug #2: portfolio path must inject market_exit_time_in_force=DAY
    just like the single-strategy path. A 1-member portfolio of AAPL.NASDAQ
    must get the TIF override."""
    member = _make_member(
        instruments=["AAPL.NASDAQ"],
        strategy_config={"instruments": ["AAPL.NASDAQ"], "fast_ema_period": 10},
    )
    config = build_portfolio_trading_node_config(
        deployment_slug=_SLUG,
        strategy_members=[member],
        ib_settings=_IB,
    )
    assert len(config.strategies) == 1
    assert config.strategies[0].config["market_exit_time_in_force"] == int(TimeInForce.DAY)


def test_portfolio_tif_not_injected_for_futures_member() -> None:
    """Non-US-equity members keep Nautilus's GTC default."""
    member = _make_member(
        instruments=["ESM4.CME"],
        strategy_config={"instruments": ["ESM4.CME"]},
        strategy_id_full="FuturesStrategy-0-abcd1234abcd1234",
    )
    config = build_portfolio_trading_node_config(
        deployment_slug=_SLUG,
        strategy_members=[member],
        ib_settings=_IB,
    )
    assert "market_exit_time_in_force" not in config.strategies[0].config


def test_portfolio_tif_per_member_independent() -> None:
    """In a mixed portfolio (one US-equity, one futures), each member
    gets its own TIF decision based on its own instruments."""
    equity = _make_member(
        instruments=["AAPL.NASDAQ"],
        strategy_config={"instruments": ["AAPL.NASDAQ"]},
        strategy_id_full="EquityStrategy-0-abcd1234abcd1234",
    )
    futures = _make_member(
        instruments=["ESM4.CME"],
        strategy_config={"instruments": ["ESM4.CME"]},
        strategy_id_full="FuturesStrategy-1-abcd1234abcd1234",
    )
    config = build_portfolio_trading_node_config(
        deployment_slug=_SLUG,
        strategy_members=[equity, futures],
        ib_settings=_IB,
    )
    assert len(config.strategies) == 2
    # Strategies are emitted in member order — assert each independently.
    equity_cfg = config.strategies[0].config
    futures_cfg = config.strategies[1].config
    assert equity_cfg["market_exit_time_in_force"] == int(TimeInForce.DAY)
    assert "market_exit_time_in_force" not in futures_cfg
