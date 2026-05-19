"use client";

/**
 * Portfolios — list-and-redirect page.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H8. The previous
 * implementation embedded a 26KB JSON-config compose dialog rejected by
 * Pablo during the iter-3 walkthrough (quote in
 * `docs/decisions/2026-05-17-portfolio-backtest-deferred.md`). Composition
 * now lives at `/portfolio/new` (form-based, no JSON), and run results at
 * `/portfolio/runs/[runId]`.
 *
 * This page is the dashboard entry point: it lists portfolios + recent
 * runs and pushes users to `/portfolio/new` for create. Zero `<Textarea>`
 * by design.
 */

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PieChart, Plus, Briefcase, ExternalLink } from "lucide-react";
import {
  apiGet,
  ApiError,
  type PortfolioResponse,
  type PortfolioListResponse,
  type PortfolioRunResponse,
  type PortfolioRunListResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime, formatCurrency } from "@/lib/format";
import { statusColor } from "@/lib/status";

function objectiveLabel(objective: string): string {
  switch (objective) {
    case "maximize_sharpe":
      return "Max Sharpe";
    case "equal_weight":
      return "Equal Weight";
    case "manual":
      return "Manual";
    default:
      return objective;
  }
}

function objectiveColor(objective: string): string {
  switch (objective) {
    case "maximize_sharpe":
      return "bg-violet-500/15 text-violet-500";
    case "equal_weight":
      return "bg-sky-500/15 text-sky-500";
    case "manual":
      return "bg-zinc-500/15 text-zinc-400";
    default:
      return "bg-muted text-muted-foreground";
  }
}

/**
 * Adaptive percent formatter — for micro-return portfolio backtests
 * the previous ``.toFixed(1)`` collapsed both Return and Drawdown to
 * "0.0%" / "-0.0%" (e.g. total_return = 9e-06 = 0.0009%). Pick the
 * decimal count so the displayed value is non-zero whenever the
 * underlying ratio is.
 */
function pctAdaptive(ratio: number): string {
  const pct = ratio * 100;
  if (pct === 0) return "0%";
  const abs = Math.abs(pct);
  let decimals = 1;
  if (abs < 0.01) decimals = 4;
  else if (abs < 0.1) decimals = 3;
  else if (abs < 1) decimals = 2;
  return `${pct.toFixed(decimals)}%`;
}

function metricsSnippet(metrics: Record<string, unknown> | null): string {
  if (!metrics) return "--";
  const parts: string[] = [];
  // Backend returns ``sharpe`` (not ``sharpe_ratio``); checking the
  // wrong key meant the Sharpe ratio NEVER rendered in this snippet
  // before — Pablo audit 2026-05-17.
  if (typeof metrics.sharpe === "number")
    parts.push(`S: ${metrics.sharpe.toFixed(2)}`);
  if (typeof metrics.total_return === "number")
    parts.push(`R: ${pctAdaptive(metrics.total_return as number)}`);
  if (typeof metrics.max_drawdown === "number")
    parts.push(`DD: ${pctAdaptive(metrics.max_drawdown as number)}`);
  return parts.length > 0 ? parts.join(" | ") : "--";
}

export default function PortfolioPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [portfolios, setPortfolios] = useState<PortfolioResponse[]>([]);
  const [runs, setRuns] = useState<PortfolioRunResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    try {
      const token = await getToken();
      const [pfData, runsData] = await Promise.all([
        apiGet<PortfolioListResponse>("/api/v1/portfolios", token),
        apiGet<PortfolioRunListResponse>("/api/v1/portfolios/runs", token),
      ]);
      setPortfolios(pfData.items);
      setRuns(runsData.items);
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load portfolios (${err.status})`
          : "Failed to load portfolios";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    let cancelled = false;
    const doLoad = async (): Promise<void> => {
      await load();
      if (cancelled) return;
    };
    void doLoad();
    return () => {
      cancelled = true;
    };
  }, [load]);

  // Sort runs newest-first
  const sortedRuns = [...runs].sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );

  const portfolioNameById: Record<string, string> = {};
  for (const pf of portfolios) {
    portfolioNameById[pf.id] = pf.name;
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Portfolios</h1>
          <p className="text-sm text-muted-foreground">
            Weighted strategy allocations with combined backtest runs
          </p>
        </div>
        <Button asChild size="sm" data-testid="portfolio-new-link">
          <Link href="/portfolio/new">
            <Plus className="mr-1.5 size-3.5" />
            New Portfolio
          </Link>
        </Button>
      </div>

      {/* Error banner */}
      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Portfolios table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Portfolios</CardTitle>
          <CardDescription>
            Strategy allocations and portfolio configurations
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading portfolios...
            </div>
          ) : portfolios.length === 0 ? (
            <div className="flex h-40 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
              <Briefcase className="size-8 opacity-40" />
              <p>No portfolios yet.</p>
              <Button asChild size="sm" variant="outline">
                <Link href="/portfolio/new">
                  <Plus className="mr-1.5 size-3.5" />
                  Compose your first portfolio
                </Link>
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Name</TableHead>
                  <TableHead>Objective</TableHead>
                  <TableHead className="text-right">Capital</TableHead>
                  <TableHead className="text-right">Leverage</TableHead>
                  <TableHead>Benchmark</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {portfolios.map((pf) => (
                  <TableRow
                    key={pf.id}
                    className="border-border/50"
                    data-testid={`portfolio-row-${pf.id}`}
                  >
                    <TableCell>
                      <div>
                        <p className="font-medium">{pf.name}</p>
                        {pf.description && (
                          <p className="max-w-xs truncate text-xs text-muted-foreground">
                            {pf.description}
                          </p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={objectiveColor(pf.objective)}
                      >
                        {objectiveLabel(pf.objective)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {formatCurrency(pf.base_capital)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {pf.requested_leverage.toFixed(1)}x
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {pf.benchmark_symbol ?? "--"}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDateTime(pf.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Recent Runs table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Recent Runs</CardTitle>
          <CardDescription>
            Combined portfolio backtest results across all portfolios
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading runs...
            </div>
          ) : sortedRuns.length === 0 ? (
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <PieChart className="size-8 opacity-40" />
              <p>No runs yet. Compose a portfolio to backtest it.</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Portfolio</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Date Range</TableHead>
                  <TableHead>Metrics</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedRuns.map((run) => (
                  <TableRow key={run.id} className="border-border/50">
                    <TableCell className="font-medium">
                      {portfolioNameById[run.portfolio_id] ??
                        run.portfolio_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={statusColor(run.status)}
                      >
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {run.start_date} &rarr; {run.end_date}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {metricsSnippet(run.metrics)}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDateTime(run.created_at)}
                    </TableCell>
                    <TableCell>
                      <Button
                        asChild
                        variant="ghost"
                        size="icon-xs"
                        aria-label="View portfolio run results"
                      >
                        <Link
                          href={`/portfolio/runs/${run.id}`}
                          data-testid={`portfolio-run-link-${run.id}`}
                        >
                          <ExternalLink className="size-3.5" />
                        </Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
