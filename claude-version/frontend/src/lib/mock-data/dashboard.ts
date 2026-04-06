/**
 * Mock data for the Dashboard page.
 */

export interface EquityPoint {
  date: string;
  value: number;
}

export interface RecentTrade {
  id: string;
  timestamp: string;
  instrument: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  pnl: number;
}

export interface ActiveStrategy {
  id: string;
  name: string;
  status: "running" | "stopped" | "error";
  instruments: string[];
  dailyPnl: number;
}

export function generateEquityCurve(): EquityPoint[] {
  const points: EquityPoint[] = [];
  let value = 112_000;
  const now = new Date();

  for (let i = 29; i >= 0; i--) {
    const date = new Date(now);
    date.setDate(date.getDate() - i);
    value += (Math.random() - 0.42) * 800;
    points.push({
      date: date.toISOString().split("T")[0],
      value: Math.round(value * 100) / 100,
    });
  }

  // Ensure last value matches target
  points[points.length - 1].value = 125_430.56;
  return points;
}

export const recentTrades: RecentTrade[] = [
  {
    id: "t1",
    timestamp: "2026-02-25T14:32:00Z",
    instrument: "AAPL",
    side: "BUY",
    quantity: 50,
    price: 189.32,
    pnl: 234.5,
  },
  {
    id: "t2",
    timestamp: "2026-02-25T14:15:00Z",
    instrument: "SPY",
    side: "SELL",
    quantity: 100,
    price: 502.15,
    pnl: -89.0,
  },
  {
    id: "t3",
    timestamp: "2026-02-25T13:48:00Z",
    instrument: "MSFT",
    side: "BUY",
    quantity: 30,
    price: 415.67,
    pnl: 156.3,
  },
  {
    id: "t4",
    timestamp: "2026-02-25T12:22:00Z",
    instrument: "ES",
    side: "SELL",
    quantity: 2,
    price: 5045.25,
    pnl: 450.0,
  },
  {
    id: "t5",
    timestamp: "2026-02-25T11:05:00Z",
    instrument: "BTC",
    side: "BUY",
    quantity: 0.5,
    price: 62_150.0,
    pnl: 312.75,
  },
  {
    id: "t6",
    timestamp: "2026-02-25T10:30:00Z",
    instrument: "AAPL",
    side: "SELL",
    quantity: 25,
    price: 188.95,
    pnl: -45.25,
  },
  {
    id: "t7",
    timestamp: "2026-02-24T15:45:00Z",
    instrument: "SPY",
    side: "BUY",
    quantity: 200,
    price: 501.32,
    pnl: 166.0,
  },
  {
    id: "t8",
    timestamp: "2026-02-24T14:10:00Z",
    instrument: "MSFT",
    side: "SELL",
    quantity: 40,
    price: 416.2,
    pnl: 212.0,
  },
  {
    id: "t9",
    timestamp: "2026-02-24T11:55:00Z",
    instrument: "ES",
    side: "BUY",
    quantity: 1,
    price: 5038.5,
    pnl: -67.5,
  },
  {
    id: "t10",
    timestamp: "2026-02-24T10:20:00Z",
    instrument: "BTC",
    side: "SELL",
    quantity: 0.25,
    price: 61_980.0,
    pnl: 85.0,
  },
];

export const activeStrategies: ActiveStrategy[] = [
  {
    id: "s1",
    name: "EMA Cross",
    status: "running",
    instruments: ["AAPL", "MSFT", "SPY"],
    dailyPnl: 523.45,
  },
  {
    id: "s2",
    name: "Mean Reversion",
    status: "running",
    instruments: ["ES", "SPY"],
    dailyPnl: 312.11,
  },
  {
    id: "s3",
    name: "Momentum",
    status: "running",
    instruments: ["BTC"],
    dailyPnl: 399.0,
  },
  {
    id: "s4",
    name: "Pairs Trading",
    status: "stopped",
    instruments: ["AAPL", "MSFT"],
    dailyPnl: 0,
  },
  {
    id: "s5",
    name: "Volatility Breakout",
    status: "error",
    instruments: ["SPY", "ES"],
    dailyPnl: 0,
  },
];
