"""Round-trip unit test for EMACrossStrategy.on_save / on_load
(Phase 4 task 4.5).

The plan also calls for a two-leg ``BacktestNode`` integration
test against testcontainers Redis. That test exists at
``tests/integration/test_ema_cross_restart_continuity.py`` and
exercises the full Nautilus kernel ``save_state`` /
``load_state`` round-trip path through the cache backend. THIS
file is the cheap, fast unit test that locks down the on_save /
on_load contract independently of the kernel — so a regression
in the dict shape (e.g. someone renames a key) is caught
immediately, without spinning up a Postgres container.

We construct a strategy, drive it directly via the
``update_raw`` indicator API to seed values, capture the
``on_save`` dict, construct a fresh strategy, hand the dict
to ``on_load``, and assert the new instance has the same
indicator state and the same ``_last_decision_bar_ts_ns``
idempotency key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# backend/tests/unit/test_ema_cross_save_load_roundtrip.py →
# up four to the claude-version root where ``strategies/``
# lives. Same pattern test_live_node_config.py uses.
_strategies_parent = str(Path(__file__).resolve().parents[3])
if _strategies_parent not in sys.path:
    sys.path.insert(0, _strategies_parent)

from strategies.example.config import EMACrossConfig  # noqa: E402
from strategies.example.ema_cross import EMACrossStrategy  # noqa: E402


def _build_strategy() -> EMACrossStrategy:
    config = EMACrossConfig(
        instrument_id="AAPL.NASDAQ",
        bar_type="AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        fast_ema_period=10,
        slow_ema_period=20,
        trade_size=100,
    )
    return EMACrossStrategy(config=config)


def test_on_save_returns_versioned_dict() -> None:
    strat = _build_strategy()
    state = strat.on_save()

    # Schema marker locks down the format so a future change
    # to the dict shape forces a code change in on_load too.
    # v3 dropped last_position_state (it was dead state —
    # Codex batch 10 P1 iter 2 fix). Reconciliation handles
    # portfolio recovery on restart.
    assert state["version"] == b"3"
    assert "fast_ema_value" in state
    assert "slow_ema_value" in state
    assert "fast_ema_count" in state
    assert "slow_ema_count" in state
    assert "last_decision_bar_ts" in state
    # Dropped in v3
    assert "last_position_state" not in state


def test_on_save_default_state_serializes() -> None:
    """Even a freshly-constructed strategy with no warmup
    should produce a valid on_save dict — the indicator
    values are zero, count is zero, and the last decision
    ts is zero. Important so a strategy that crashes
    immediately after startup still writes something
    on_load can read back."""
    strat = _build_strategy()
    state = strat.on_save()

    assert state["fast_ema_value"] == b"0.0"
    assert state["slow_ema_value"] == b"0.0"
    assert state["fast_ema_count"] == b"0"
    assert state["slow_ema_count"] == b"0"
    assert state["last_decision_bar_ts"] == b"0"


def test_round_trip_restores_indicator_values() -> None:
    """The classic round-trip: warm a strategy, save, build a
    fresh instance, load — assert the indicator readings
    match."""
    strat = _build_strategy()
    strat.fast_ema.update_raw(100.5)
    strat.slow_ema.update_raw(99.2)
    strat._last_decision_bar_ts_ns = 1_700_000_000_000_000_000  # noqa: SLF001

    state = strat.on_save()

    fresh = _build_strategy()
    fresh.on_load(state)

    assert fresh.fast_ema.value == pytest.approx(100.5)
    assert fresh.slow_ema.value == pytest.approx(99.2)
    assert fresh._last_decision_bar_ts_ns == 1_700_000_000_000_000_000  # noqa: SLF001


def test_round_trip_count_above_period_preserved_exactly() -> None:
    """Codex batch 10 P3 iter 2 regression: previously
    on_load capped replay at ``period``, so a saved count
    of 11 reloaded as 10. The new behavior replays
    exactly ``count`` times so the count round-trips
    exactly. Exact-fidelity matters because the count is
    persisted on the next save and a drift could compound
    across restarts."""
    strat = _build_strategy()
    # Drive the fast EMA past its period (10) so the saved
    # count is 11
    for _ in range(11):
        strat.fast_ema.update_raw(100.0)
    for _ in range(20):
        strat.slow_ema.update_raw(99.0)
    assert strat.fast_ema.count == 11
    assert strat.slow_ema.count == 20

    state = strat.on_save()
    fresh = _build_strategy()
    fresh.on_load(state)

    # Counts round-trip exactly, no period cap
    assert fresh.fast_ema.count == 11
    assert fresh.slow_ema.count == 20


def test_on_load_empty_state_is_cold_start() -> None:
    """An empty dict means "no prior state" — strategy
    starts at default values without raising."""
    fresh = _build_strategy()
    fresh.on_load({})
    # Indicators stay uninitialized
    assert fresh.fast_ema.initialized is False
    assert fresh.slow_ema.initialized is False


def test_on_load_wrong_version_is_cold_start() -> None:
    """Defensive against schema drift — a state dict from a
    different version is treated as cold start, NOT crashed
    on. The next bar warms the indicators normally."""
    fresh = _build_strategy()
    fresh.on_load(
        {
            "version": b"99",
            "fast_ema_value": b"100",
            "slow_ema_value": b"99",
            "fast_ema_count": b"10",
            "slow_ema_count": b"20",
            "last_decision_bar_ts": b"1",
        }
    )
    assert fresh.fast_ema.initialized is False


def test_on_load_v2_state_treated_as_cold_start() -> None:
    """A v2 state dict (with last_position_state) loaded
    against v3 code must cold-start cleanly rather than
    crash. Defensive against in-place upgrades where
    Redis still has the old format."""
    fresh = _build_strategy()
    fresh.on_load(
        {
            "version": b"2",
            "fast_ema_value": b"100",
            "slow_ema_value": b"99",
            "fast_ema_count": b"10",
            "slow_ema_count": b"20",
            "last_position_state": b"LONG",
            "last_decision_bar_ts": b"1",
        }
    )
    assert fresh.fast_ema.initialized is False


def test_on_load_malformed_state_is_cold_start() -> None:
    """If the cache returns garbage (corrupted bytes,
    non-decodable values), fall back to cold start instead
    of crashing the strategy."""
    fresh = _build_strategy()
    fresh.on_load(
        {
            "version": b"3",
            "fast_ema_value": b"not-a-float",
            "slow_ema_value": b"99",
            "fast_ema_count": b"10",
            "slow_ema_count": b"20",
            "last_decision_bar_ts": b"1",
        }
    )
    assert fresh.fast_ema.initialized is False


def test_on_load_zero_last_decision_ts_becomes_none() -> None:
    """The on_save side encodes ``None`` as ``b"0"`` (a
    sentinel for 'no prior decision'). on_load must convert
    it back to ``None`` so the next bar's
    ``ts_event > self._last_decision_bar_ts_ns`` check works
    correctly (None compares as 'no prior bar', not as 0)."""
    fresh = _build_strategy()
    fresh.on_load(
        {
            "version": b"3",
            "fast_ema_value": b"100",
            "slow_ema_value": b"99",
            "fast_ema_count": b"10",
            "slow_ema_count": b"20",
            "last_decision_bar_ts": b"0",
        }
    )
    assert fresh._last_decision_bar_ts_ns is None  # noqa: SLF001


def test_on_load_marks_initialized_indicators_initialized() -> None:
    """If the pre-restart strategy had the indicator
    initialized (count >= period), the post-restart
    strategy must also report initialized so the next bar
    runs the cross detection."""
    strat = _build_strategy()
    # Drive the indicators above the period so they flip to
    # initialized BEFORE we save state.
    for _ in range(strat.fast_ema.period):
        strat.fast_ema.update_raw(100.0)
    for _ in range(strat.slow_ema.period):
        strat.slow_ema.update_raw(99.0)
    assert strat.fast_ema.initialized is True
    state = strat.on_save()

    fresh = _build_strategy()
    fresh.on_load(state)
    assert fresh.fast_ema.initialized is True
    assert fresh.slow_ema.initialized is True


def test_on_load_preserves_uninitialized_state() -> None:
    """Codex batch 10 P1 regression: a pre-restart strategy
    with ``count=1`` (only one bar seen so far) must report
    ``initialized=False`` after restart. Earlier code blindly
    flipped initialized to True regardless of count, which
    would let the next post-restart bar trigger a false
    cross signal."""
    strat = _build_strategy()
    strat.fast_ema.update_raw(100.0)  # count=1, NOT initialized
    strat.slow_ema.update_raw(99.0)  # count=1, NOT initialized
    assert strat.fast_ema.initialized is False
    assert strat.slow_ema.initialized is False

    state = strat.on_save()
    fresh = _build_strategy()
    fresh.on_load(state)

    # Restored strategy MUST also be uninitialized
    assert fresh.fast_ema.initialized is False
    assert fresh.slow_ema.initialized is False
    # And the count was preserved
    assert fresh.fast_ema.count == 1
    assert fresh.slow_ema.count == 1
