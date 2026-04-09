"use client";

import { useEffect, useState } from "react";
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

type BacktestAnalytics = {
  id: string;
  metrics: Record<string, number>;
  series: Array<{
    timestamp: string;
    returns: number;
    equity: number;
    drawdown: number;
  }>;
  report_url?: string | null;
};

export default function BacktestResultPage() {
  const { id } = useParams<{ id: string }>();
  const { token } = useAuth();
  const [status, setStatus] = useState<BacktestStatus | null>(null);
  const [results, setResults] = useState<BacktestResults | null>(null);
  const [analytics, setAnalytics] = useState<BacktestAnalytics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token || !id) return;

    let timer: number | null = null;

    async function poll() {
      try {
        const current = await apiFetch<BacktestStatus>(`/api/v1/backtests/${id}/status`, token);
        setStatus(current);
        if (current.status === "completed" || current.status === "failed") {
          const [result, analyticsPayload] = await Promise.all([
            apiFetch<BacktestResults>(`/api/v1/backtests/${id}/results`, token),
            apiFetch<BacktestAnalytics>(`/api/v1/backtests/${id}/analytics`, token).catch(() => null),
          ]);
          setResults({
            ...result,
            trades: result.trades.map((trade) => ({
              ...trade,
              executed_at: trade.executed_at,
              pnl: trade.pnl ?? 0,
            })),
          });
          setAnalytics(analyticsPayload);
          return;
        }
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load backtest";
        setError(message);
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

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-white/10 bg-black/25 p-4">
        <p className="text-sm text-zinc-300">Job {id}</p>
        <p className="mt-1 text-xl font-semibold text-white">{status?.status ?? "pending"}</p>
        <p className="text-sm text-zinc-400">Progress: {status?.progress ?? 0}%</p>
        {analytics?.report_url ? (
          <a
            href={`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}${analytics.report_url}`}
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex rounded-xl border border-cyan-300/30 px-3 py-2 text-sm text-cyan-100"
          >
            Open HTML Report
          </a>
        ) : null}
      </div>
      {error ? <div className="rounded-lg border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div> : null}
      {results ? (
        <>
          <ResultsCharts
            metrics={analytics?.metrics ?? results.metrics ?? { sharpe: 0, sortino: 0, max_drawdown: 0 }}
            series={analytics?.series ?? []}
          />
          {analytics && analytics.series.length === 0 ? (
            <div className="rounded-lg border border-dashed border-white/10 bg-black/20 p-4 text-sm text-zinc-400">
              This backtest completed, but no equity series was persisted for charting. The metric snapshot is real; rerun
              the backtest if you need a refreshed report artifact.
            </div>
          ) : null}
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
