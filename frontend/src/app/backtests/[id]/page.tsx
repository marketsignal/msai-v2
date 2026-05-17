"use client";

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Download, Loader2 } from "lucide-react";
import {
  ResultsCharts,
  type ResultsChartsBacktest,
} from "@/components/backtests/results-charts";
import { FailureCard } from "@/components/backtests/failure-card";
import { ReportIframe } from "@/components/backtests/report-iframe";
import { TradeLog } from "@/components/backtests/trade-log";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  apiFetch,
  apiGet,
  ApiError,
  describeApiError,
  type BacktestResultsResponse,
  type BacktestStatusResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

const MAX_RESULTS_RETRIES = 10; // 10 × 3s = 30s wall-clock window

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
  const { getToken } = useAuth();
  const [status, setStatus] = useState<BacktestStatusResponse | null>(null);
  const [results, setResults] = useState<BacktestResultsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [notFound, setNotFound] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let timerId: ReturnType<typeof setTimeout> | null = null;
    // Local `let` (not useState) — useEffect deps are [id, getToken] only,
    // so a useState counter would close over its initial value and the
    // exhaustion branch would never fire. A local `let` owned by this
    // effect instance persists across poll() invocations correctly.
    let resultsRetries = 0;
    let isInitialLoad = true;

    const poll = async (): Promise<void> => {
      if (!active) return;
      try {
        const token = await getToken();
        const fresh = await apiGet<BacktestStatusResponse>(
          `/api/v1/backtests/${encodeURIComponent(id)}/status`,
          token,
        );
        if (!active) return;
        if (isInitialLoad) {
          setStatus(fresh);
          isInitialLoad = false;
        } else {
          // Shallow-compare guard: polling fires every 3s; avoid needless
          // re-renders when the status payload is unchanged.
          setStatus((prev) => {
            if (
              prev &&
              prev.id === fresh.id &&
              prev.status === fresh.status &&
              prev.phase === fresh.phase &&
              prev.progress_message === fresh.progress_message &&
              prev.progress === fresh.progress &&
              prev.completed_at === fresh.completed_at &&
              prev.started_at === fresh.started_at
            ) {
              return prev; // no-op — React bails out on same reference
            }
            return fresh;
          });
        }
        setLoading(false);
        // On running → completed, fetch /results. If /results transiently
        // 404s (race between status-commit and results-commit in the worker),
        // keep polling up to a bounded retry budget —
        // MAX_RESULTS_RETRIES × 3s = 30s window. On success OR exhaustion,
        // stop polling; exhaustion leaves status="completed" visible and
        // metrics=null until manual refresh.
        if (fresh.status === "completed") {
          try {
            const results = await apiGet<BacktestResultsResponse>(
              `/api/v1/backtests/${encodeURIComponent(id)}/results`,
              token,
            );
            if (!active) return;
            setResults(results);
            setError(null);
            return; // success — stop polling
          } catch (resultsErr) {
            if (!active) return;
            if (resultsRetries >= MAX_RESULTS_RETRIES) {
              // Budget exhausted. Surface the error so the user sees a
              // clear banner instead of a silently-empty "completed"
              // shell with no body and no actionable message. The
              // render path still shows the header + back button via
              // the error route.
              const msg =
                resultsErr instanceof ApiError
                  ? `Results unavailable (HTTP ${resultsErr.status}). Try refreshing the page.`
                  : "Results unavailable. Try refreshing the page.";
              setError(msg);
              return;
            }
            resultsRetries += 1;
            timerId = setTimeout(() => {
              void poll();
            }, 3000);
            return;
          }
        }
        if (fresh.status === "failed") {
          return; // terminal — stop polling
        }
        if (fresh.status === "pending" || fresh.status === "running") {
          timerId = setTimeout(() => {
            void poll();
          }, 3000);
        }
      } catch (err) {
        if (!active) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
          setLoading(false);
          return;
        }
        const msg =
          err instanceof ApiError
            ? `Failed to load backtest (${err.status})`
            : "Failed to load backtest";
        setError(msg);
        // Clear loading so the error UI + back-navigation render on
        // repeated API failures; otherwise the user is stuck on the
        // "Loading backtest..." spinner forever. Codex review P2 2026-04-21.
        setLoading(false);
        timerId = setTimeout(() => {
          void poll();
        }, 5000);
      }
    };

    void poll();
    return () => {
      active = false;
      if (timerId !== null) clearTimeout(timerId);
    };
  }, [id, getToken]);

  const handleDownloadReport = async (): Promise<void> => {
    try {
      const token = await getToken();
      const res = await apiFetch(
        `/api/v1/backtests/${encodeURIComponent(id)}/report`,
        {},
        token,
      );
      if (!res.ok) {
        // iter-3 SF P2: throw ApiError with the parsed body so the catch
        // site's ``describeApiError`` can extract the backend's detail
        // (e.g. "report_signing_secret_unset", "backtest_not_found").
        // Previously the throw stripped the body and only the status
        // survived.
        let body: unknown = null;
        try {
          body = await res.json();
        } catch {
          // ignore — body may be empty or non-JSON; ApiError tolerates null
        }
        throw new ApiError(`Download failed: ${res.status}`, res.status, body);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `backtest-${id}.html`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Report download failed:", err);
      // iter-3 describeApiError sweep.
      setError(
        `Report download failed: ${describeApiError(err, "Unknown error")}`,
      );
    }
  };

  // Adapt API metrics (ratios 0-1) into the percent shape ResultsCharts expects.
  const backtestForCharts: ResultsChartsBacktest | null = useMemo(() => {
    if (!results || !results.metrics) return null;
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
            {status.phase === "awaiting_data" && (
              <div
                data-testid="backtest-phase-indicator"
                className="mt-1 flex items-center gap-2 text-sm text-muted-foreground"
              >
                <Loader2 className="h-3 w-3 animate-spin" />
                <span data-testid="backtest-phase-message">
                  {status.progress_message || "Downloading data…"}
                </span>
              </div>
            )}
          </div>
        </div>
        {status.status === "completed" && (
          <Button
            variant="outline"
            className="gap-1.5"
            onClick={handleDownloadReport}
          >
            <Download className="size-3.5" />
            Download Report
          </Button>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {status.status === "failed" && status.error ? (
        <FailureCard error={status.error} />
      ) : status.status !== "completed" ? (
        <div className="flex h-48 items-center justify-center rounded-md border border-border/50 text-sm text-muted-foreground">
          {status.status === "failed"
            ? "Backtest failed. Check worker logs for details."
            : status.status === "running"
              ? `Backtest in progress (${Math.round((status.progress ?? 0) * 100)}%)`
              : "Backtest not yet complete."}
        </div>
      ) : results && !results.metrics ? (
        <div className="flex h-48 items-center justify-center rounded-md border border-border/50 text-sm text-muted-foreground">
          Backtest completed but no metrics are available. The worker may have
          failed to populate results.
        </div>
      ) : backtestForCharts ? (
        <Tabs defaultValue="native" className="mt-2">
          <TabsList>
            <TabsTrigger value="native">Native view</TabsTrigger>
            <TabsTrigger
              value="full_report"
              disabled={!results?.has_report}
              data-testid="tab-full-report"
            >
              Full report
            </TabsTrigger>
          </TabsList>
          <TabsContent value="native" className="space-y-6">
            <ResultsCharts
              backtest={backtestForCharts}
              series={results?.series ?? null}
              seriesStatus={results?.series_status ?? "not_materialized"}
            />
            <TradeLog backtestId={id} />
          </TabsContent>
          <TabsContent value="full_report">
            <ReportIframe
              backtestId={id}
              hasReport={Boolean(results?.has_report)}
            />
          </TabsContent>
        </Tabs>
      ) : null}
    </div>
  );
}
