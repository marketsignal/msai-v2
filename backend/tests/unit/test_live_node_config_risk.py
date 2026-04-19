"""Risk-engine configuration tests for the live trading node
(Phase 3 task 3.8).

Verifies that ``build_live_trading_node_config`` populates
Nautilus's built-in :class:`LiveRiskEngineConfig` with real
native throttles instead of leaving everything at the
defaults. The custom checks (per-strategy max position, daily
loss, kill switch, market hours) are NOT here — they're in
the :class:`RiskAwareStrategy` mixin from Task 3.7. This task
only configures what Nautilus natively supports:

- ``bypass=False`` so the engine actually runs
- ``max_order_submit_rate`` for the submit-rate throttle
- ``max_order_modify_rate`` for the modify-rate throttle
- ``max_notional_per_order`` for per-instrument dollar caps
"""

from __future__ import annotations

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_live_trading_node_config,
)

_DEFAULT_KWARGS = {
    "deployment_slug": "abcd1234abcd1234",
    "strategy_path": "strategies.example.ema_cross:EMACrossStrategy",
    "strategy_config_path": "strategies.example.config:EMACrossConfig",
    "strategy_config": {},
    "paper_symbols": ["AAPL"],
    "ib_settings": IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567"),
}


class TestLiveRiskEngineDefaults:
    """The default configuration must NEVER bypass the engine
    and must keep Nautilus's native rate limits in place."""

    def test_bypass_is_false(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.risk_engine is not None
        assert config.risk_engine.bypass is False

    def test_default_submit_rate_is_100_per_second(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.risk_engine.max_order_submit_rate == "100/00:00:01"

    def test_default_modify_rate_is_100_per_second(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.risk_engine.max_order_modify_rate == "100/00:00:01"

    def test_default_max_notional_per_order_is_empty_dict(self) -> None:
        """No caller-supplied caps → empty dict (not None) so
        Nautilus's engine treats every instrument as
        unrestricted at the per-order level. The custom
        per-strategy max position lives in the RiskAwareStrategy
        mixin from Task 3.7."""
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.risk_engine.max_notional_per_order == {}


class TestLiveRiskEngineCustomLimits:
    """The function must accept caller-supplied limits and
    forward them verbatim to ``LiveRiskEngineConfig``."""

    def test_max_notional_per_order_is_forwarded(self) -> None:
        caps = {"AAPL.NASDAQ": 100_000, "MSFT.NASDAQ": 50_000}
        config = build_live_trading_node_config(
            **_DEFAULT_KWARGS,
            max_notional_per_order=caps,
        )
        assert config.risk_engine.max_notional_per_order == caps

    def test_max_order_submit_rate_is_forwarded(self) -> None:
        config = build_live_trading_node_config(
            **_DEFAULT_KWARGS,
            max_order_submit_rate="10/00:00:01",
        )
        assert config.risk_engine.max_order_submit_rate == "10/00:00:01"

    def test_max_order_modify_rate_is_forwarded(self) -> None:
        config = build_live_trading_node_config(
            **_DEFAULT_KWARGS,
            max_order_modify_rate="5/00:00:01",
        )
        assert config.risk_engine.max_order_modify_rate == "5/00:00:01"

    def test_none_max_notional_collapses_to_empty_dict(self) -> None:
        """A test for the explicit-None branch — distinguishes
        a caller passing ``None`` (use defaults) from a caller
        passing an empty dict (still defaults). Both must yield
        an empty dict, not a TypeError on Nautilus's side."""
        config = build_live_trading_node_config(
            **_DEFAULT_KWARGS,
            max_notional_per_order=None,
        )
        assert config.risk_engine.max_notional_per_order == {}


class TestRiskEngineIsAlwaysPresentInLiveConfig:
    """A live trading node MUST have a risk_engine. This is the
    inverse of the backtest case: in backtests we deliberately
    skip the live engine, but live nodes must always carry one."""

    def test_risk_engine_is_not_none(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.risk_engine is not None
