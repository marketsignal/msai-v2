"use client";

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Download } from "lucide-react";
import {
  ResultsCharts,
  type ResultsChartsBacktest,
} from "@/components/backtests/results-charts";
import { TradeLog } from "@/components/backtests/trade-log";
import {
  apiGet,
  ApiError,
  type BacktestResultsResponse,
  type BacktestStatusResponse,
} from "@/lib/api";
import { generateEquityCurve, backtestTrades } from "@/lib/mock-data/backtests";

function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "pending":
      return "bg-amber-500/15 text-amber-500 hover:bg-amber-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export default function BacktestDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const [status, setStatus] = useState<BacktestStatusResponse | null>(null);
  const [results, setResults] = useState<BacktestResultsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [notFound, setNotFound] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      setLoading(true);
      setError(null);
      try {
        const statusData = await apiGet<BacktestStatusResponse>(
          `/api/v1/backtests/${encodeURIComponent(id)}/status`,
        );
        if (cancelled) return;
        setStatus(statusData);

        if (statusData.status === "completed") {
          const resultsData = await apiGet<BacktestResultsResponse>(
            `/api/v1/backtests/${encodeURIComponent(id)}/results`,
          );
          if (cancelled) return;
          setResults(resultsData);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          const msg =
            err instanceof ApiError
              ? `Failed to load backtest (${err.status})`
              : "Failed to load backtest";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Adapt API metrics (ratios 0-1) into the percent shape ResultsCharts expects.
  const backtestForCharts: ResultsChartsBacktest | null = useMemo(() => {
    if (!results) return null;
    const m = results.metrics;
    return {
      sharpeRatio: m.sharpe_ratio,
      sortinoRatio: m.sortino_ratio,
      maxDrawdown: m.max_drawdown * 100,
      totalReturn: m.total_return * 100,
      winRate: m.win_rate * 100,
      totalTrades: m.num_trades,
    };
  }, [results]);

  // The backend results endpoint does not yet return an equity curve or
  // a trade log, so we mock those for now from the total return percent.
  const equityCurve = useMemo(
    () =>
      backtestForCharts
        ? generateEquityCurve(backtestForCharts.totalReturn)
        : [],
    [backtestForCharts],
  );

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center text-sm text-muted-foreground">
        Loading backtest...
      </div>
    );
  }

  if (notFound || !status) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">{error ?? "Backtest not found"}</p>
        <Button asChild variant="outline">
          <Link href="/backtests">Back to Backtests</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => router.push("/backtests")}
          >
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                Backtest {status.id.slice(0, 8)}
              </h1>
              <Badge variant="secondary" className={statusColor(status.status)}>
                {status.status}
              </Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {status.started_at
                ? `Started ${new Date(status.started_at).toLocaleString()}`
                : "Not started"}
              {status.completed_at
                ? ` • Completed ${new Date(status.completed_at).toLocaleString()}`
                : ""}
            </p>
          </div>
        </div>
        {status.status === "completed" && (
          <Button variant="outline" className="gap-1.5" asChild>
            <a
              href={`/api/v1/backtests/${status.id}/report`}
              target="_blank"
              rel="noreferrer"
            >
              <Download className="size-3.5" />
              Download Report
            </a>
          </Button>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {status.status !== "completed" ? (
        <div className="flex h-48 items-center justify-center rounded-md border border-border/50 text-sm text-muted-foreground">
          {status.status === "failed"
            ? "Backtest failed. Check worker logs for details."
            : status.status === "running"
              ? `Backtest in progress (${Math.round((status.progress ?? 0) * 100)}%)`
              : "Backtest not yet complete."}
        </div>
      ) : backtestForCharts ? (
        <>
          <ResultsCharts
            backtest={backtestForCharts}
            equityCurve={equityCurve}
          />
          <TradeLog trades={backtestTrades} />
        </>
      ) : null}
    </div>
  );
}
