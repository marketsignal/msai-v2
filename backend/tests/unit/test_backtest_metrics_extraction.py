"""Regression tests for ``_extract_metrics`` (drill 2026-04-15).

The first-real-backtest milestone surfaced a silent bug: a backtest
that produced 4 448 fills returned ``win_rate=0``, ``sharpe=0``,
``total_return≈0`` because Nautilus's aggregate ``stats_pnls`` /
``stats_returns`` came back NaN. The extraction function only
consulted those stats and the account snapshot — never the per-
position ``realized_pnl`` that Nautilus's ``positions_df`` always
carries.

These tests assert the three-tier fallback:

1. When Nautilus stats are present, use them as-is.
2. When Nautilus stats are zero/NaN but ``positions_df`` has closed
   positions with realized_pnl, derive metrics from positions.
3. When neither is usable, fall through to the account-series tier
   (covered by other tests).
"""

from __future__ import annotations

import pandas as pd

from msai.services.nautilus.backtest_runner import (
    _derive_metrics_from_positions,
    _extract_metrics,
    _money_to_float,
)


class _FakePrimary:
    def __init__(
        self, stats_returns: dict[str, float], stats_pnls: dict[str, dict[str, float]]
    ) -> None:
        self.stats_returns = stats_returns
        self.stats_pnls = stats_pnls


def _orders(n: int) -> pd.DataFrame:
    return pd.DataFrame({"side": ["BUY", "SELL"] * (n // 2)})


def _positions(pnls: list[float]) -> pd.DataFrame:
    """Build a positions DataFrame with the columns Nautilus's
    ``generate_positions_report`` produces — only the ones the
    derivation logic touches."""
    return pd.DataFrame(
        {
            "side": ["FLAT"] * len(pnls),
            "realized_pnl": [f"{p:.2f} USD" for p in pnls],
            "avg_px_open": [100.0] * len(pnls),
            "peak_qty": [1] * len(pnls),
        }
    )


# ---------------------------------------------------------------------------
# Tier 1 — Nautilus stats present
# ---------------------------------------------------------------------------


def test_uses_nautilus_stats_when_present() -> None:
    """Happy path: Nautilus reported real numbers; pass them through."""
    primary = _FakePrimary(
        stats_returns={
            "Sharpe Ratio (252 days)": 1.42,
            "Sortino Ratio (252 days)": 2.10,
            "Max Drawdown": -0.085,
        },
        stats_pnls={"USD": {"PnL% (total)": 0.142, "Win Rate": 0.563}},
    )
    metrics = _extract_metrics(primary, _orders(20), pd.DataFrame(), pd.DataFrame())
    assert metrics["sharpe_ratio"] == 1.42
    assert metrics["sortino_ratio"] == 2.10
    assert metrics["max_drawdown"] == -0.085
    assert metrics["total_return"] == 0.142
    assert metrics["win_rate"] == 0.563
    assert metrics["num_trades"] == 20


# ---------------------------------------------------------------------------
# Tier 2 — Positions-derived fallback (the drill bug)
# ---------------------------------------------------------------------------


def test_falls_back_to_positions_when_nautilus_stats_are_nan() -> None:
    """Drill 2026-04-15 root cause: Nautilus ``stats_pnls`` is empty,
    ``stats_returns`` is all NaN, but ``positions_df`` carries the
    actual per-position realized_pnl. Without this fallback every
    backtest report on the dashboard reads as ``win_rate=0``.

    Synthetic positions: 3 wins (+1.0 each), 2 losses (-0.5 each)
    → win_rate = 3/5 = 0.6.
    """
    primary = _FakePrimary(
        stats_returns={
            "Sharpe Ratio (252 days)": float("nan"),
            "Sortino Ratio (252 days)": float("nan"),
            "Max Drawdown": float("nan"),
        },
        stats_pnls={},
    )
    positions = _positions([1.0, 1.0, 1.0, -0.5, -0.5])

    metrics = _extract_metrics(primary, _orders(10), pd.DataFrame(), positions)
    assert metrics["win_rate"] == 0.6
    # total realized_pnl = +2.0 ; notional = 5 × 100 = 500 → ratio 0.004
    assert abs(metrics["total_return"] - 0.004) < 1e-9
    # cumulative path: +1, +2, +3, +2.5, +2.0 — running max stays at +3
    # → drawdown bottoms at -1.0 / 500 = -0.002
    assert abs(metrics["max_drawdown"] - -0.002) < 1e-9


def test_falls_back_to_positions_when_nautilus_stats_are_zero() -> None:
    """Same behaviour as the NaN case for engines that return literal
    0.0 instead of NaN. Both shapes mean "no aggregate available"."""
    primary = _FakePrimary(
        stats_returns={"Sharpe Ratio": 0.0, "Sortino Ratio": 0.0, "Max Drawdown": 0.0},
        stats_pnls={"USD": {"PnL% (total)": 0.0, "Win Rate": 0.0}},
    )
    positions = _positions([2.0, -1.0])
    metrics = _extract_metrics(primary, _orders(4), pd.DataFrame(), positions)
    assert metrics["win_rate"] == 0.5
    assert metrics["total_return"] != 0.0


def test_open_positions_excluded_from_derivation() -> None:
    """``side != 'FLAT'`` means the position is still open and its
    PnL is unrealized — must not be included in win_rate or total."""
    positions = pd.DataFrame(
        {
            "side": ["FLAT", "FLAT", "LONG"],
            "realized_pnl": ["1.00 USD", "-0.50 USD", "9999.00 USD"],
            "avg_px_open": [100.0, 100.0, 100.0],
            "peak_qty": [1, 1, 1],
        }
    )
    derived = _derive_metrics_from_positions(positions)
    assert derived is not None
    assert derived["win_rate"] == 0.5  # 1/2, NOT 1/3 or 2/3
    # +1.0 + -0.5 = +0.5 over 200 notional = 0.0025
    assert abs(derived["total_return"] - 0.0025) < 1e-9


def test_derive_returns_none_for_empty_or_all_open_positions() -> None:
    assert _derive_metrics_from_positions(pd.DataFrame()) is None
    all_open = pd.DataFrame(
        {
            "side": ["LONG", "SHORT"],
            "realized_pnl": ["1.00 USD", "-1.00 USD"],
            "avg_px_open": [100.0, 100.0],
            "peak_qty": [1, 1],
        }
    )
    assert _derive_metrics_from_positions(all_open) is None


# ---------------------------------------------------------------------------
# Money parsing
# ---------------------------------------------------------------------------


def test_money_to_float_handles_nautilus_money_string() -> None:
    """Nautilus renders ``Money(0.11, USD)`` as ``"0.11 USD"``."""
    assert _money_to_float("0.11 USD") == 0.11
    assert _money_to_float("-3.06 USD") == -3.06
    assert _money_to_float("123") == 123.0
    assert _money_to_float(0.5) == 0.5
    assert _money_to_float(None) is None
    assert _money_to_float("") is None
    assert _money_to_float("garbage") is None


# ---------------------------------------------------------------------------
# Codex review fixes
# ---------------------------------------------------------------------------


def test_account_tier_overrides_positions_for_total_return_and_drawdown() -> None:
    """Codex review P1 regression: when both account_df and positions
    can supply total_return/max_drawdown, the account tier wins
    because it captures portfolio-level capital recycling and open-
    position drag that the positions tier (realized PnL only)
    misses. Win_rate still comes from positions because account
    snapshots don't carry per-trade win/loss.
    """
    primary = _FakePrimary(
        stats_returns={"Sharpe": 0.0, "Sortino": 0.0, "Max Drawdown": 0.0},
        stats_pnls={},
    )
    # account_df with one row whose returns column is non-trivial.
    # _derive_metrics_from_account uses the returns column + a
    # timestamp-like column to compute a series.
    account_df = pd.DataFrame(
        {
            "ts_event": pd.to_datetime(
                ["2026-01-01 09:30", "2026-01-01 09:31", "2026-01-01 09:32"], utc=True
            ),
            "returns": [0.0, 0.05, -0.02],
        }
    )
    # Positions tier would suggest total_return = 0.001 (if used).
    positions = _positions([1.0, -0.5])

    metrics = _extract_metrics(primary, _orders(4), account_df, positions)
    # Win rate must come from positions (1 win out of 2 → 0.5)
    assert metrics["win_rate"] == 0.5
    # Total return must NOT match the positions-derived approximation
    # because the account tier provided a value.
    positions_derived = _derive_metrics_from_positions(positions)
    assert positions_derived is not None
    assert metrics["total_return"] != positions_derived["total_return"]


def test_drawdown_seeds_with_zero_so_first_trade_loss_is_captured() -> None:
    """Codex review P2 regression: an immediately-losing strategy
    should report a non-zero max_drawdown from trade one. Before the
    fix, ``cumulative.cummax()`` adopted the first negative value as
    the peak, so drawdown stayed at 0 until the series first made a
    new high (which never happens for a strictly-losing run)."""
    # All losses → without seed, max_drawdown=0 (wrong); with seed,
    # peak starts at 0 and drawdown bottoms at the cumulative low.
    positions = _positions([-0.5, -0.3, -0.2])
    derived = _derive_metrics_from_positions(positions)
    assert derived is not None
    assert derived["max_drawdown"] < 0, (
        f"strictly-losing run must have negative max_drawdown; got {derived['max_drawdown']}"
    )
    # cumulative path with seed: 0, -0.5, -0.8, -1.0; peak = 0;
    # drawdown bottoms at -1.0 / notional (3 × 100 = 300) ≈ -0.00333
    assert abs(derived["max_drawdown"] - (-1.0 / 300.0)) < 1e-9


def test_mixed_currency_positions_only_aggregate_dominant_currency() -> None:
    """Codex review P2 regression: a multi-currency positions report
    must NOT add USD + EUR amounts as if they were the same unit.
    Pick the dominant currency (most rows) and aggregate only that
    bucket so the totals stay meaningful."""
    positions = pd.DataFrame(
        {
            "side": ["FLAT"] * 5,
            "realized_pnl": [
                "1.00 USD",
                "1.00 USD",
                "1.00 USD",  # USD: 3 rows, sum 3.0
                "9999.00 EUR",
                "-9999.00 EUR",  # EUR: 2 rows, would add chaos to USD
            ],
            "avg_px_open": [100.0] * 5,
            "peak_qty": [1] * 5,
        }
    )
    derived = _derive_metrics_from_positions(positions)
    assert derived is not None
    # Only the 3 USD rows participate. All wins → win_rate = 1.0.
    # If EUR had leaked in, win_rate would be 4/5 = 0.8 (4 wins of 5).
    assert derived["win_rate"] == 1.0
    # Total PnL = 3.0 USD over notional 3 × 100 = 300 → 0.01.
    # If EUR had leaked the sum would be wildly different.
    assert abs(derived["total_return"] - 0.01) < 1e-9
