"""Recovery + state-persistence configuration tests
(Phase 4 task 4.1).

Verifies that ``build_live_trading_node_config`` enables
Nautilus's built-in state persistence and the in-flight /
position reconciliation knobs needed for production-grade
recovery. Without these flags set explicitly:

- ``load_state`` and ``save_state`` BOTH default to False on
  ``TradingNodeConfig`` despite the docstring claiming True
  (Codex gotcha #10). Forgetting to flip them is the silent
  path to a restart that resets every strategy's internal
  state to first-bar defaults.
- The in-flight order watchdog runs Nautilus's defaults
  which are sane (2s/5s) but we set them explicitly so a
  future Nautilus default change doesn't silently relax our
  checks.
- ``position_check_interval_secs`` defaults to ``None`` —
  i.e. the periodic position reconciliation against the
  broker is OFF by default. We turn it on at 60s.
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


class TestStatePersistence:
    """Phase 4 task 4.1: ``load_state`` and ``save_state``
    must be explicitly True so a restarted subprocess picks up
    where the previous one left off."""

    def test_load_state_is_true(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.load_state is True

    def test_save_state_is_true(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.save_state is True


class TestReconciliation:
    """Phase 1 enabled startup reconciliation; Phase 4 keeps
    it enabled and adds the in-flight + position-drift checks."""

    def test_reconciliation_enabled(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.exec_engine.reconciliation is True

    def test_reconciliation_lookback_24h(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        # 24 hours so a long weekend gap doesn't lose orders
        assert config.exec_engine.reconciliation_lookback_mins == 1440


class TestInflightOrderWatchdog:
    """Phase 4 task 4.1: pin the in-flight watchdog cadence
    so a future Nautilus default change can't silently relax
    our checks."""

    def test_inflight_check_interval_2s(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.exec_engine.inflight_check_interval_ms == 2000

    def test_inflight_check_threshold_5s(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.exec_engine.inflight_check_threshold_ms == 5000


class TestPositionReconciliation:
    """Phase 4 task 4.1: Nautilus defaults
    ``position_check_interval_secs`` to ``None`` (OFF). The
    builder MUST set a real interval so position drift
    against the broker is detected."""

    def test_position_check_interval_60s(self) -> None:
        config = build_live_trading_node_config(**_DEFAULT_KWARGS)
        assert config.exec_engine.position_check_interval_secs == 60
