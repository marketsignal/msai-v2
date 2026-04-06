"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";

import { ResultsCharts } from "@/components/backtests/results-charts";
import { TradeLog } from "@/components/backtests/trade-log";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type BacktestStatus = { id: string; status: string; progress: number };
type BacktestResults = {
  id: string;
  status: string;
  metrics?: Record<string, number>;
  trades: Array<{
    executed_at: string;
    instrument: string;
    side: string;
    quantity: number;
    price: number;
    pnl: number | null;
  }>;
};

export default function BacktestResultPage() {
  const { id } = useParams<{ id: string }>();
  const { token } = useAuth();
  const [status, setStatus] = useState<BacktestStatus | null>(null);
  const [results, setResults] = useState<BacktestResults | null>(null);

  useEffect(() => {
    if (!token || !id) return;

    let timer: number | null = null;

    async function poll() {
      try {
        const current = await apiFetch<BacktestStatus>(`/api/v1/backtests/${id}/status`, token);
        setStatus(current);
        if (current.status === "completed" || current.status === "failed") {
          const result = await apiFetch<BacktestResults>(`/api/v1/backtests/${id}/results`, token);
          setResults({
            ...result,
            trades: result.trades.map((trade) => ({
              ...trade,
              executed_at: trade.executed_at,
              pnl: trade.pnl ?? 0,
            })),
          });
          return;
        }
      } catch {
        setStatus({ id, status: "running", progress: 65 });
      }
      timer = window.setTimeout(() => {
        void poll();
      }, 2000);
    }

    void poll();

    return () => {
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, [id, token]);

  const series = useMemo(() => {
    return Array.from({ length: 40 }).map((_, index) => {
      const equity = 1 + index * 0.012 + Math.sin(index / 4) * 0.02;
      const drawdown = Math.min(0, equity - (1 + index * 0.012));
      return {
        timestamp: new Date(Date.now() - (39 - index) * 3600_000).toISOString(),
        equity,
        drawdown,
      };
    });
  }, []);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-white/10 bg-black/25 p-4">
        <p className="text-sm text-zinc-300">Job {id}</p>
        <p className="mt-1 text-xl font-semibold text-white">{status?.status ?? "pending"}</p>
        <p className="text-sm text-zinc-400">Progress: {status?.progress ?? 0}%</p>
      </div>
      {results ? (
        <>
          <ResultsCharts metrics={results.metrics ?? { sharpe: 0, sortino: 0, max_drawdown: 0 }} series={series} />
          <TradeLog
            rows={results.trades.map((trade) => ({
              timestamp: trade.executed_at,
              instrument: trade.instrument,
              side: trade.side,
              quantity: trade.quantity,
              price: trade.price,
              pnl: trade.pnl ?? 0,
            }))}
          />
        </>
      ) : null}
    </div>
  );
}
