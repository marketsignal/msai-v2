"""Assert ``build_portfolio_trading_node_config`` always sets
``load_state=True`` and ``save_state=True``.

Without these flags, a restarted Nautilus subprocess quietly resets every
strategy's internal state (EMA values, position tracking, etc.) to
first-bar defaults instead of rehydrating from Redis. This is the
single most catastrophic silent failure mode for warm restarts of
portfolio deployments (gotcha #10).

The existing ``test_trading_node_factory.py`` already covers
``test_build_config_preserves_load_state_save_state_true`` but with
inline assertions inside a broader test. These dedicated regression
tests make the invariant explicit and un-missable in CI output.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_portfolio_trading_node_config,
)
from msai.services.nautilus.trading_node_subprocess import StrategyMemberPayload


def _make_member(
    *,
    instruments: list[str] | None = None,
    strategy_path: str = "strategies.example.ema_cross:EMACrossStrategy",
    strategy_config_path: str = "strategies.example.config:EMACrossConfig",
    strategy_config: dict[str, Any] | None = None,
    strategy_id_full: str = "",
) -> StrategyMemberPayload:
    return StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path=strategy_path,
        strategy_config_path=strategy_config_path,
        strategy_config=strategy_config if strategy_config is not None else {},
        strategy_id_full=strategy_id_full,
        instruments=instruments if instruments is not None else ["AAPL"],
    )


_DEFAULT_IB = IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567")


def test_portfolio_config_sets_load_state_true() -> None:
    """``load_state=True`` so a restarted subprocess rehydrates strategy
    state from the Redis-backed cache instead of resetting to defaults."""
    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[_make_member()],
        ib_settings=_DEFAULT_IB,
    )
    assert config.load_state is True, (
        "load_state must be True — without it, warm restart silently "
        "discards persisted strategy state (gotcha #10)"
    )


def test_portfolio_config_sets_save_state_true() -> None:
    """``save_state=True`` so the subprocess persists strategy state to
    Redis on every state change, ready for rehydration on next start."""
    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[_make_member()],
        ib_settings=_DEFAULT_IB,
    )
    assert config.save_state is True, (
        "save_state must be True — without it, the subprocess never "
        "writes strategy state to Redis and warm restart has nothing "
        "to load (gotcha #10)"
    )


def test_portfolio_config_with_multiple_members_preserves_flags() -> None:
    """The persistence flags must hold regardless of how many strategy
    members the portfolio contains. Regression guard against a future
    code path that constructs the config differently for N > 1."""
    m1 = _make_member(instruments=["AAPL"], strategy_id_full="s1@slug")
    m2 = _make_member(instruments=["MSFT"], strategy_id_full="s2@slug")
    m3 = _make_member(instruments=["SPY"], strategy_id_full="s3@slug")

    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[m1, m2, m3],
        ib_settings=_DEFAULT_IB,
    )
    assert config.load_state is True
    assert config.save_state is True
