"""Simplified backtesting engine for MSAI v2 Phase 1.

Runs a strategy against historical bar data and produces a :class:`BacktestResult`
with orders, positions, equity curve, and performance metrics.  Full NautilusTrader
engine integration is planned for Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from msai.core.logging import get_logger

log = get_logger(__name__)

_TRADING_DAYS_PER_YEAR = 252
_RISK_FREE_RATE = 0.0


@dataclass
class BacktestResult:
    """Container for backtest output data.

    Attributes:
        orders_df: DataFrame of all orders generated (timestamp, side, price, quantity).
        positions_df: DataFrame tracking position changes over time.
        account_df: DataFrame with equity curve (timestamp, equity, cash, position_value).
        metrics: Dictionary of performance metrics (sharpe, sortino, max_drawdown, etc.).
        returns_series: Daily returns series for QuantStats reporting.
    """

    orders_df: pd.DataFrame
    positions_df: pd.DataFrame
    account_df: pd.DataFrame
    metrics: dict[str, Any]
    returns_series: pd.Series  # type: ignore[type-arg]


class BacktestRunner:
    """Runs a strategy against historical data and produces results.

    This is a simplified event-driven engine for Phase 1. It iterates
    through bars chronologically, calls ``strategy.on_bar(bar)`` for each,
    and tracks positions, trades, and the equity curve.
    """

    def run(
        self,
        strategy_class: type,
        config: dict[str, Any],
        bars_df: pd.DataFrame,
        initial_cash: float = 100_000.0,
    ) -> BacktestResult:
        """Run a backtest simulation.

        Args:
            strategy_class: The strategy class to instantiate (must have ``on_bar`` method).
            config: Configuration dict passed as kwargs to the strategy constructor.
            bars_df: DataFrame with at least ``timestamp``, ``open``, ``high``,
                ``low``, ``close``, ``volume`` columns sorted by timestamp.
            initial_cash: Starting cash balance.

        Returns:
            A :class:`BacktestResult` with all simulation outputs.
        """
        strategy = strategy_class(**config)

        orders: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        account_history: list[dict[str, Any]] = []

        cash = initial_cash
        position_qty = 0.0
        position_avg_price = 0.0

        for _, row in bars_df.iterrows():
            bar: dict[str, Any] = row.to_dict()
            current_price = float(bar["close"])
            timestamp = bar.get("timestamp", None)

            # Feed bar to strategy
            signal = strategy.on_bar(bar)

            if signal is not None:
                side: str = signal["side"]
                price = float(signal["price"])
                qty = float(signal["quantity"])

                if side == "BUY":
                    cost = price * qty
                    if cost <= cash:
                        cash -= cost
                        position_qty += qty
                        if position_qty > 0:
                            position_avg_price = price
                        orders.append(
                            {
                                "timestamp": timestamp,
                                "side": side,
                                "price": price,
                                "quantity": qty,
                            }
                        )
                elif side == "SELL":
                    if position_qty > 0:
                        sell_qty = min(qty, position_qty)
                        cash += price * sell_qty
                        position_qty -= sell_qty
                        orders.append(
                            {
                                "timestamp": timestamp,
                                "side": side,
                                "price": price,
                                "quantity": sell_qty,
                            }
                        )
                    elif position_qty == 0:
                        # Allow short selling in simplified model
                        position_qty -= qty
                        cash += price * qty
                        position_avg_price = price
                        orders.append(
                            {
                                "timestamp": timestamp,
                                "side": side,
                                "price": price,
                                "quantity": qty,
                            }
                        )

            # Track position state
            positions.append(
                {
                    "timestamp": timestamp,
                    "position_qty": position_qty,
                    "avg_price": position_avg_price,
                }
            )

            # Track equity curve
            position_value = position_qty * current_price
            equity = cash + position_value
            account_history.append(
                {
                    "timestamp": timestamp,
                    "equity": equity,
                    "cash": cash,
                    "position_value": position_value,
                }
            )

        orders_df = pd.DataFrame(orders)
        positions_df = pd.DataFrame(positions)
        account_df = pd.DataFrame(account_history)

        returns_series = self._compute_returns(account_df)
        metrics = self._compute_metrics(returns_series, orders_df, initial_cash, account_df)

        log.info(
            "backtest_completed",
            num_trades=len(orders),
            total_return=metrics.get("total_return", 0.0),
            sharpe_ratio=metrics.get("sharpe_ratio", 0.0),
        )

        return BacktestResult(
            orders_df=orders_df,
            positions_df=positions_df,
            account_df=account_df,
            metrics=metrics,
            returns_series=returns_series,
        )

    def _compute_returns(self, account_df: pd.DataFrame) -> pd.Series:  # type: ignore[type-arg]
        """Compute percentage returns from the equity curve.

        Args:
            account_df: DataFrame with ``timestamp`` and ``equity`` columns.

        Returns:
            A pandas Series of period-over-period returns, indexed by timestamp.
        """
        if account_df.empty or "equity" not in account_df.columns:
            return pd.Series(dtype=float)

        equity = account_df["equity"]
        returns = equity.pct_change().fillna(0.0)

        if "timestamp" in account_df.columns:
            returns.index = pd.to_datetime(account_df["timestamp"])

        return returns

    def _compute_metrics(
        self,
        returns: pd.Series,  # type: ignore[type-arg]
        orders_df: pd.DataFrame,
        initial_cash: float,
        account_df: pd.DataFrame,
    ) -> dict[str, Any]:
        """Calculate performance metrics from returns and trade data.

        Args:
            returns: Period returns series.
            orders_df: DataFrame of executed orders.
            initial_cash: Starting cash balance.
            account_df: Equity curve DataFrame.

        Returns:
            Dictionary of performance metrics.
        """
        metrics: dict[str, Any] = {
            "total_return": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "num_trades": 0,
            "initial_cash": initial_cash,
            "final_equity": initial_cash,
        }

        if account_df.empty:
            return metrics

        final_equity = float(account_df["equity"].iloc[-1])
        metrics["final_equity"] = final_equity
        metrics["total_return"] = (final_equity - initial_cash) / initial_cash

        num_trades = len(orders_df)
        metrics["num_trades"] = num_trades

        # Sharpe ratio (annualized)
        if len(returns) > 1:
            mean_return = float(np.mean(returns))
            std_return = float(np.std(returns, ddof=1))
            if std_return > 0:
                metrics["sharpe_ratio"] = (
                    (mean_return - _RISK_FREE_RATE / _TRADING_DAYS_PER_YEAR)
                    / std_return
                    * np.sqrt(_TRADING_DAYS_PER_YEAR)
                )

            # Sortino ratio (annualized, using downside deviation)
            downside = returns[returns < 0]
            if len(downside) > 0:
                downside_std = float(np.std(downside, ddof=1))
                if downside_std > 0:
                    metrics["sortino_ratio"] = (
                        (mean_return - _RISK_FREE_RATE / _TRADING_DAYS_PER_YEAR)
                        / downside_std
                        * np.sqrt(_TRADING_DAYS_PER_YEAR)
                    )

        # Max drawdown
        if "equity" in account_df.columns and len(account_df) > 0:
            equity_series = account_df["equity"].values
            running_max = np.maximum.accumulate(equity_series)
            drawdowns = (equity_series - running_max) / running_max
            metrics["max_drawdown"] = float(np.min(drawdowns))

        # Win rate based on round-trip trades
        if num_trades >= 2:
            metrics["win_rate"] = self._compute_win_rate(orders_df)

        return metrics

    def _compute_win_rate(self, orders_df: pd.DataFrame) -> float:
        """Compute win rate from paired buy/sell orders.

        Args:
            orders_df: DataFrame with ``side`` and ``price`` columns.

        Returns:
            Fraction of round-trip trades that were profitable (0.0 to 1.0).
        """
        buys: list[float] = []
        wins = 0
        total_round_trips = 0

        for _, order in orders_df.iterrows():
            if order["side"] == "BUY":
                buys.append(float(order["price"]))
            elif order["side"] == "SELL" and buys:
                buy_price = buys.pop(0)
                sell_price = float(order["price"])
                total_round_trips += 1
                if sell_price > buy_price:
                    wins += 1

        if total_round_trips == 0:
            return 0.0
        return wins / total_round_trips
