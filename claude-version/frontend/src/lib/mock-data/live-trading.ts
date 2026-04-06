/**
 * Mock data for the Live Trading page.
 */

export interface Deployment {
  id: string;
  strategyName: string;
  strategyId: string;
  instruments: string[];
  status: "running" | "stopped" | "error";
  startTime: string;
  dailyPnl: number;
  totalPnl: number;
}

export interface Position {
  id: string;
  instrument: string;
  side: "LONG" | "SHORT";
  quantity: number;
  avgPrice: number;
  currentPrice: number;
  unrealizedPnl: number;
  marketValue: number;
}

export const deployments: Deployment[] = [
  {
    id: "dep-1",
    strategyName: "EMA Cross",
    strategyId: "ema-cross",
    instruments: ["AAPL", "MSFT", "SPY"],
    status: "running",
    startTime: "2026-02-20T08:00:00Z",
    dailyPnl: 523.45,
    totalPnl: 3_245.8,
  },
  {
    id: "dep-2",
    strategyName: "Mean Reversion",
    strategyId: "mean-reversion",
    instruments: ["ES", "SPY"],
    status: "running",
    startTime: "2026-02-22T09:30:00Z",
    dailyPnl: 312.11,
    totalPnl: 1_876.55,
  },
];

export const positions: Position[] = [
  {
    id: "pos-1",
    instrument: "AAPL",
    side: "LONG",
    quantity: 150,
    avgPrice: 186.45,
    currentPrice: 189.32,
    unrealizedPnl: 430.5,
    marketValue: 28_398.0,
  },
  {
    id: "pos-2",
    instrument: "MSFT",
    side: "LONG",
    quantity: 50,
    avgPrice: 412.3,
    currentPrice: 415.67,
    unrealizedPnl: 168.5,
    marketValue: 20_783.5,
  },
  {
    id: "pos-3",
    instrument: "SPY",
    side: "SHORT",
    quantity: 100,
    avgPrice: 504.8,
    currentPrice: 502.15,
    unrealizedPnl: 265.0,
    marketValue: 50_215.0,
  },
  {
    id: "pos-4",
    instrument: "ES",
    side: "LONG",
    quantity: 2,
    avgPrice: 5032.0,
    currentPrice: 5045.25,
    unrealizedPnl: 26.5,
    marketValue: 10_090.5,
  },
  {
    id: "pos-5",
    instrument: "MSFT",
    side: "SHORT",
    quantity: 30,
    avgPrice: 418.5,
    currentPrice: 415.67,
    unrealizedPnl: 84.9,
    marketValue: 12_470.1,
  },
];
