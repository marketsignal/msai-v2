"use client";

import { useEffect, useMemo, useState } from "react";

import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { RecentTrades } from "@/components/dashboard/recent-trades";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type AccountSummary = {
  net_liquidation: number;
  buying_power: number;
  margin_used: number;
  available_funds: number;
  unrealized_pnl: number;
};

type LiveStatus = {
  id: string;
  strategy: string;
  status: "running" | "stopped" | "error";
  daily_pnl: number;
};

type LiveTrade = {
  id: string;
  executed_at: string;
  instrument: string;
  side: string;
  quantity: number;
  price: number;
  pnl: number;
};

export default function DashboardPage() {
  const { token } = useAuth();
  const [summary, setSummary] = useState<AccountSummary>({
    net_liquidation: 1_250_000,
    buying_power: 650_000,
    margin_used: 200_000,
    available_funds: 450_000,
    unrealized_pnl: 980,
  });
  const [strategies, setStrategies] = useState<LiveStatus[]>([]);
  const [trades, setTrades] = useState<LiveTrade[]>([]);

  useEffect(() => {
    if (!token) {
      return;
    }

    async function load() {
      try {
        const [account, status, recentTrades] = await Promise.all([
          apiFetch<AccountSummary>("/api/v1/account/summary", token),
          apiFetch<LiveStatus[]>("/api/v1/live/status", token),
          apiFetch<LiveTrade[]>("/api/v1/live/trades", token),
        ]);
        setSummary(account);
        setStrategies(status);
        setTrades(recentTrades);
      } catch {
        setStrategies([
          { id: "dep-1", strategy: "EMA Cross", status: "running", daily_pnl: 342.4 },
          { id: "dep-2", strategy: "Mean Reversion", status: "stopped", daily_pnl: -24.8 },
        ]);
        setTrades([
          {
            id: "t1",
            executed_at: new Date().toISOString(),
            instrument: "AAPL",
            side: "BUY",
            quantity: 10,
            price: 211.2,
            pnl: 34.2,
          },
        ]);
      }
    }

    void load();
  }, [token]);

  const equity = useMemo(() => {
    return Array.from({ length: 30 }).map((_, index) => ({
      timestamp: new Date(Date.now() - (29 - index) * 86_400_000).toISOString(),
      value: 1_000_000 + index * 4200 + Math.sin(index / 2) * 5200,
    }));
  }, []);

  return (
    <div className="space-y-6">
      <PortfolioSummary
        totalValue={summary.net_liquidation}
        dailyPnl={summary.unrealized_pnl}
        totalReturn={summary.net_liquidation > 0 ? summary.unrealized_pnl / summary.net_liquidation : 0}
        activeStrategies={strategies.filter((item) => item.status === "running").length}
      />
      <div className="grid gap-6 xl:grid-cols-[1.3fr_1fr]">
        <EquityChart data={equity} />
        <ActiveStrategies
          items={strategies.map((strategy) => ({
            id: strategy.id,
            name: strategy.strategy,
            status: strategy.status,
            dailyPnl: strategy.daily_pnl,
          }))}
        />
      </div>
      <RecentTrades
        items={trades.map((item) => ({
          id: item.id,
          timestamp: item.executed_at,
          instrument: item.instrument,
          side: item.side,
          quantity: item.quantity,
          price: item.price,
          pnl: item.pnl,
        }))}
      />
    </div>
  );
}
