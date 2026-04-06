/**
 * Mock data for the Strategies page.
 */

export interface Strategy {
  id: string;
  name: string;
  description: string;
  status: "running" | "stopped" | "error";
  sharpeRatio: number;
  totalReturn: number;
  winRate: number;
  instruments: string[];
  config: Record<string, unknown>;
  backtestHistory: BacktestSummary[];
}

export interface BacktestSummary {
  id: string;
  dateRange: string;
  status: "completed" | "running" | "failed";
  sharpeRatio: number;
  totalReturn: number;
  runDate: string;
}

export const strategies: Strategy[] = [
  {
    id: "ema-cross",
    name: "EMA Cross",
    description:
      "Crossover strategy using exponential moving averages (12/26 periods). Goes long when fast EMA crosses above slow EMA, and short on the inverse. Includes ATR-based stop losses and position sizing.",
    status: "running",
    sharpeRatio: 1.82,
    totalReturn: 24.5,
    winRate: 62.3,
    instruments: ["AAPL", "MSFT", "SPY"],
    config: {
      fast_period: 12,
      slow_period: 26,
      atr_multiplier: 2.0,
      position_size_pct: 0.02,
      max_positions: 5,
      stop_loss_pct: 0.03,
      take_profit_pct: 0.06,
    },
    backtestHistory: [
      {
        id: "bt-1",
        dateRange: "2025-01-01 to 2025-12-31",
        status: "completed",
        sharpeRatio: 1.82,
        totalReturn: 24.5,
        runDate: "2026-02-20",
      },
      {
        id: "bt-2",
        dateRange: "2024-01-01 to 2024-12-31",
        status: "completed",
        sharpeRatio: 1.54,
        totalReturn: 18.2,
        runDate: "2026-02-15",
      },
      {
        id: "bt-3",
        dateRange: "2023-06-01 to 2024-06-01",
        status: "completed",
        sharpeRatio: 1.67,
        totalReturn: 21.0,
        runDate: "2026-02-10",
      },
    ],
  },
  {
    id: "mean-reversion",
    name: "Mean Reversion",
    description:
      "Statistical arbitrage strategy based on Bollinger Band mean reversion. Enters positions when price deviates 2+ standard deviations from the 20-period moving average. Uses RSI confirmation filter.",
    status: "running",
    sharpeRatio: 1.45,
    totalReturn: 16.8,
    winRate: 71.5,
    instruments: ["ES", "SPY"],
    config: {
      lookback_period: 20,
      entry_std_devs: 2.0,
      exit_std_devs: 0.5,
      rsi_period: 14,
      rsi_oversold: 30,
      rsi_overbought: 70,
      position_size_pct: 0.03,
    },
    backtestHistory: [
      {
        id: "bt-4",
        dateRange: "2025-01-01 to 2025-12-31",
        status: "completed",
        sharpeRatio: 1.45,
        totalReturn: 16.8,
        runDate: "2026-02-18",
      },
      {
        id: "bt-5",
        dateRange: "2024-06-01 to 2025-06-01",
        status: "failed",
        sharpeRatio: 0,
        totalReturn: 0,
        runDate: "2026-02-12",
      },
    ],
  },
  {
    id: "momentum",
    name: "Momentum",
    description:
      "Trend-following strategy using rate of change (ROC) and ADX filters. Enters long positions when 12-period ROC is positive and ADX > 25, indicating strong trending conditions. Trailing stop exits.",
    status: "stopped",
    sharpeRatio: 2.14,
    totalReturn: 31.2,
    winRate: 55.8,
    instruments: ["BTC", "ETH"],
    config: {
      roc_period: 12,
      adx_period: 14,
      adx_threshold: 25,
      trailing_stop_pct: 0.05,
      position_size_pct: 0.01,
      max_drawdown_pct: 0.15,
    },
    backtestHistory: [
      {
        id: "bt-6",
        dateRange: "2025-01-01 to 2025-12-31",
        status: "completed",
        sharpeRatio: 2.14,
        totalReturn: 31.2,
        runDate: "2026-02-22",
      },
      {
        id: "bt-7",
        dateRange: "2024-01-01 to 2024-12-31",
        status: "completed",
        sharpeRatio: 1.89,
        totalReturn: 27.5,
        runDate: "2026-02-08",
      },
      {
        id: "bt-8",
        dateRange: "2023-01-01 to 2023-12-31",
        status: "completed",
        sharpeRatio: 1.32,
        totalReturn: 14.0,
        runDate: "2026-01-30",
      },
    ],
  },
];

export function getStrategyById(id: string): Strategy | undefined {
  return strategies.find((s) => s.id === id);
}
