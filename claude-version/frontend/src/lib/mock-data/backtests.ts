/**
 * Mock data for the Backtests page.
 */

export interface Backtest {
  id: string;
  strategyId: string;
  strategyName: string;
  dateRange: string;
  startDate: string;
  endDate: string;
  status: "completed" | "running" | "failed";
  sharpeRatio: number;
  sortinoRatio: number;
  maxDrawdown: number;
  totalReturn: number;
  winRate: number;
  totalTrades: number;
  runDate: string;
  instruments: string[];
}

export interface BacktestTrade {
  id: string;
  timestamp: string;
  instrument: string;
  side: "BUY" | "SELL";
  quantity: number;
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  holdingPeriod: string;
}

export interface EquityPoint {
  date: string;
  equity: number;
  drawdown: number;
}

export interface MonthlyReturn {
  month: string;
  year: number;
  return_pct: number;
}

export const backtests: Backtest[] = [
  {
    id: "bt-001",
    strategyId: "ema-cross",
    strategyName: "EMA Cross",
    dateRange: "2025-01-01 to 2025-12-31",
    startDate: "2025-01-01",
    endDate: "2025-12-31",
    status: "completed",
    sharpeRatio: 1.82,
    sortinoRatio: 2.45,
    maxDrawdown: -8.3,
    totalReturn: 24.5,
    winRate: 62.3,
    totalTrades: 156,
    runDate: "2026-02-20",
    instruments: ["AAPL", "MSFT", "SPY"],
  },
  {
    id: "bt-002",
    strategyId: "mean-reversion",
    strategyName: "Mean Reversion",
    dateRange: "2025-01-01 to 2025-12-31",
    startDate: "2025-01-01",
    endDate: "2025-12-31",
    status: "completed",
    sharpeRatio: 1.45,
    sortinoRatio: 1.98,
    maxDrawdown: -5.6,
    totalReturn: 16.8,
    winRate: 71.5,
    totalTrades: 234,
    runDate: "2026-02-18",
    instruments: ["ES", "SPY"],
  },
  {
    id: "bt-003",
    strategyId: "momentum",
    strategyName: "Momentum",
    dateRange: "2025-01-01 to 2025-12-31",
    startDate: "2025-01-01",
    endDate: "2025-12-31",
    status: "completed",
    sharpeRatio: 2.14,
    sortinoRatio: 3.1,
    maxDrawdown: -12.1,
    totalReturn: 31.2,
    winRate: 55.8,
    totalTrades: 89,
    runDate: "2026-02-22",
    instruments: ["BTC"],
  },
  {
    id: "bt-004",
    strategyId: "ema-cross",
    strategyName: "EMA Cross",
    dateRange: "2024-01-01 to 2024-12-31",
    startDate: "2024-01-01",
    endDate: "2024-12-31",
    status: "completed",
    sharpeRatio: 1.54,
    sortinoRatio: 2.1,
    maxDrawdown: -10.2,
    totalReturn: 18.2,
    winRate: 59.1,
    totalTrades: 142,
    runDate: "2026-02-15",
    instruments: ["AAPL", "MSFT", "SPY"],
  },
  {
    id: "bt-005",
    strategyId: "mean-reversion",
    strategyName: "Mean Reversion",
    dateRange: "2024-06-01 to 2025-06-01",
    startDate: "2024-06-01",
    endDate: "2025-06-01",
    status: "failed",
    sharpeRatio: 0,
    sortinoRatio: 0,
    maxDrawdown: 0,
    totalReturn: 0,
    winRate: 0,
    totalTrades: 0,
    runDate: "2026-02-12",
    instruments: ["ES"],
  },
  {
    id: "bt-006",
    strategyId: "momentum",
    strategyName: "Momentum",
    dateRange: "2024-01-01 to 2024-12-31",
    startDate: "2024-01-01",
    endDate: "2024-12-31",
    status: "completed",
    sharpeRatio: 1.89,
    sortinoRatio: 2.67,
    maxDrawdown: -14.5,
    totalReturn: 27.5,
    winRate: 53.2,
    totalTrades: 76,
    runDate: "2026-02-08",
    instruments: ["BTC", "ETH"],
  },
];

export function generateEquityCurve(totalReturn: number): EquityPoint[] {
  const points: EquityPoint[] = [];
  let equity = 100_000;
  const target = equity * (1 + totalReturn / 100);
  const step = (target - equity) / 250;
  let maxEquity = equity;

  for (let i = 0; i < 250; i++) {
    equity += step + (Math.random() - 0.48) * 400;
    if (equity > maxEquity) maxEquity = equity;
    const drawdown = ((equity - maxEquity) / maxEquity) * 100;
    const date = new Date(2025, 0, 1);
    date.setDate(date.getDate() + i);
    points.push({
      date: date.toISOString().split("T")[0],
      equity: Math.round(equity * 100) / 100,
      drawdown: Math.round(drawdown * 100) / 100,
    });
  }

  return points;
}

export function generateMonthlyReturns(): MonthlyReturn[] {
  const months = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];
  const returns: MonthlyReturn[] = [];
  for (const year of [2024, 2025]) {
    for (const month of months) {
      returns.push({
        month,
        year,
        return_pct: Math.round((Math.random() * 12 - 3) * 100) / 100,
      });
    }
  }
  return returns;
}

export const backtestTrades: BacktestTrade[] = [
  {
    id: "btt-1",
    timestamp: "2025-01-15T10:30:00Z",
    instrument: "AAPL",
    side: "BUY",
    quantity: 100,
    entryPrice: 175.5,
    exitPrice: 182.3,
    pnl: 680.0,
    holdingPeriod: "3d",
  },
  {
    id: "btt-2",
    timestamp: "2025-01-22T14:15:00Z",
    instrument: "MSFT",
    side: "BUY",
    quantity: 50,
    entryPrice: 395.2,
    exitPrice: 402.8,
    pnl: 380.0,
    holdingPeriod: "5d",
  },
  {
    id: "btt-3",
    timestamp: "2025-02-05T09:45:00Z",
    instrument: "SPY",
    side: "SELL",
    quantity: 200,
    entryPrice: 495.0,
    exitPrice: 490.5,
    pnl: 900.0,
    holdingPeriod: "2d",
  },
  {
    id: "btt-4",
    timestamp: "2025-02-14T11:00:00Z",
    instrument: "AAPL",
    side: "SELL",
    quantity: 75,
    entryPrice: 184.0,
    exitPrice: 186.5,
    pnl: -187.5,
    holdingPeriod: "1d",
  },
  {
    id: "btt-5",
    timestamp: "2025-03-01T13:20:00Z",
    instrument: "MSFT",
    side: "BUY",
    quantity: 60,
    entryPrice: 405.0,
    exitPrice: 412.7,
    pnl: 462.0,
    holdingPeriod: "4d",
  },
  {
    id: "btt-6",
    timestamp: "2025-03-18T10:00:00Z",
    instrument: "SPY",
    side: "BUY",
    quantity: 150,
    entryPrice: 502.3,
    exitPrice: 508.1,
    pnl: 870.0,
    holdingPeriod: "6d",
  },
  {
    id: "btt-7",
    timestamp: "2025-04-02T15:30:00Z",
    instrument: "AAPL",
    side: "BUY",
    quantity: 80,
    entryPrice: 178.9,
    exitPrice: 173.5,
    pnl: -432.0,
    holdingPeriod: "3d",
  },
  {
    id: "btt-8",
    timestamp: "2025-04-20T09:15:00Z",
    instrument: "MSFT",
    side: "SELL",
    quantity: 40,
    entryPrice: 410.0,
    exitPrice: 414.2,
    pnl: -168.0,
    holdingPeriod: "2d",
  },
  {
    id: "btt-9",
    timestamp: "2025-05-10T12:45:00Z",
    instrument: "SPY",
    side: "BUY",
    quantity: 100,
    entryPrice: 510.5,
    exitPrice: 518.3,
    pnl: 780.0,
    holdingPeriod: "5d",
  },
  {
    id: "btt-10",
    timestamp: "2025-05-28T14:00:00Z",
    instrument: "AAPL",
    side: "BUY",
    quantity: 120,
    entryPrice: 185.0,
    exitPrice: 191.2,
    pnl: 744.0,
    holdingPeriod: "7d",
  },
];

export function getBacktestById(id: string): Backtest | undefined {
  return backtests.find((b) => b.id === id);
}
