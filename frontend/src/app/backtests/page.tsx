"use client";

/**
 * Backtest history — unified list of single-strategy + portfolio runs.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H9. The G4 backend
 * `GET /api/v1/backtests/history?type=<single|portfolio|all>` returns rows
 * tagged with a ``type`` discriminator; this page renders a single
 * timeline with a type filter + Type badge per row. Click-through routes:
 *
 *   - single → /backtests/{id}
 *   - portfolio → /portfolio/runs/{run_id}
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ExternalLink } from "lucide-react";
import { RunBacktestForm } from "@/components/backtests/run-form";
import { RunSmokeButton } from "@/components/backtests/run-smoke-button";
import {
  apiGet,
  ApiError,
  type BacktestHistoryItem,
  type BacktestHistoryResponse,
  type StrategyListResponse,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDate } from "@/lib/format";

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
    case "canceled":
      return "bg-zinc-500/15 text-zinc-400 hover:bg-zinc-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

type HistoryTypeFilter = "all" | "single" | "portfolio";

function typeBadgeClass(type: BacktestHistoryItem["type"]): string {
  return type === "portfolio"
    ? "bg-violet-500/15 text-violet-500 hover:bg-violet-500/25"
    : "bg-sky-500/15 text-sky-500 hover:bg-sky-500/25";
}

function typeLabel(type: BacktestHistoryItem["type"]): string {
  return type === "portfolio" ? "Portfolio" : "Single";
}

/**
 * Compute the click-through href for a history row. Portfolio rows route
 * to the results page at ``/portfolio/runs/{id}``; single rows route to
 * ``/backtests/{id}`` as before.
 */
function detailHref(item: BacktestHistoryItem): string {
  if (item.type === "portfolio") {
    return `/portfolio/runs/${item.id}`;
  }
  return `/backtests/${item.id}`;
}

export default function BacktestsPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [runDialogOpen, setRunDialogOpen] = useState<boolean>(false);
  const [backtests, setBacktests] = useState<BacktestHistoryItem[]>([]);
  const [strategiesById, setStrategiesById] = useState<
    Record<string, StrategyResponse>
  >({});
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<HistoryTypeFilter>("all");

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const token = await getToken();
      const [history, strategies] = await Promise.all([
        apiGet<BacktestHistoryResponse>(
          `/api/v1/backtests/history?type=${encodeURIComponent(typeFilter)}`,
          token,
        ),
        apiGet<StrategyListResponse>("/api/v1/strategies/", token),
      ]);
      setBacktests(history.items);
      const map: Record<string, StrategyResponse> = {};
      for (const s of strategies.items) map[s.id] = s;
      setStrategiesById(map);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load backtests (${err.status})`
          : "Failed to load backtests";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [getToken, typeFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Backtests</h1>
          <p className="text-sm text-muted-foreground">
            Run and review historical strategy + portfolio backtests
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RunSmokeButton />
          <RunBacktestForm
            open={runDialogOpen}
            onOpenChange={setRunDialogOpen}
            onSubmitted={() => void load()}
          />
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Backtests table */}
      <Card className="border-border/50">
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div>
            <CardTitle className="text-base">Backtest History</CardTitle>
            <CardDescription>
              Unified timeline of single-strategy and portfolio backtests
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <label
              htmlFor="backtest-type-filter"
              className="text-xs text-muted-foreground"
            >
              Type
            </label>
            <Select
              value={typeFilter}
              onValueChange={(v) => setTypeFilter(v as HistoryTypeFilter)}
            >
              <SelectTrigger
                id="backtest-type-filter"
                className="h-8 w-[140px]"
                data-testid="backtest-type-filter"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all" data-testid="filter-option-all">
                  All
                </SelectItem>
                <SelectItem value="single" data-testid="filter-option-single">
                  Single
                </SelectItem>
                <SelectItem
                  value="portfolio"
                  data-testid="filter-option-portfolio"
                >
                  Portfolio
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading backtests...
            </div>
          ) : backtests.length === 0 ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              {typeFilter === "all"
                ? 'No backtests yet. Click "Run Backtest" to start one.'
                : `No ${typeFilter} backtests yet.`}
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Type</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Date Range</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {backtests.map((bt) => {
                  const isPortfolio = bt.type === "portfolio";
                  const name = isPortfolio
                    ? (bt.portfolio_name ??
                      bt.portfolio_id?.slice(0, 8) ??
                      bt.id.slice(0, 8))
                    : bt.strategy_id
                      ? (strategiesById[bt.strategy_id]?.name ??
                        bt.strategy_id.slice(0, 8))
                      : bt.id.slice(0, 8);
                  return (
                    <TableRow
                      key={bt.id}
                      className="border-border/50"
                      data-testid={`backtest-row-${bt.id}`}
                    >
                      <TableCell>
                        <Badge
                          variant="secondary"
                          className={typeBadgeClass(bt.type)}
                          data-testid={`backtest-type-${bt.id}`}
                        >
                          {typeLabel(bt.type)}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-medium">{name}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {bt.start_date} to {bt.end_date}
                      </TableCell>
                      <TableCell>
                        {bt.status === "failed" && bt.error_public_message ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              {/* tabIndex={0} + role="button" make
                                  the Badge keyboard-focusable — Radix Tooltip
                                  opens on focus (not just hover) for a11y. */}
                              <Badge
                                variant="secondary"
                                className={`${statusColor(bt.status)} cursor-help`}
                                data-testid={`backtest-status-${bt.id}`}
                                tabIndex={0}
                                role="button"
                                aria-label={`Backtest failed: ${bt.error_public_message.slice(0, 80)}`}
                              >
                                {bt.status}
                              </Badge>
                            </TooltipTrigger>
                            <TooltipContent
                              side="top"
                              className="max-w-xs whitespace-pre-wrap text-xs"
                              data-testid={`backtest-error-tooltip-${bt.id}`}
                            >
                              {bt.error_public_message.length > 150
                                ? `${bt.error_public_message.slice(0, 150)}…`
                                : bt.error_public_message}
                            </TooltipContent>
                          </Tooltip>
                        ) : (
                          <Badge
                            variant="secondary"
                            className={statusColor(bt.status)}
                          >
                            {bt.status}
                          </Badge>
                        )}
                        {bt.status === "running" &&
                          bt.phase === "awaiting_data" && (
                            <Badge
                              data-testid="backtest-list-fetching-badge"
                              variant="outline"
                              className="ml-1 text-xs"
                            >
                              Fetching data…
                            </Badge>
                          )}
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground">
                        {formatDate(bt.created_at)}
                      </TableCell>
                      <TableCell>
                        {bt.status !== "pending" && (
                          <Button
                            asChild
                            variant="ghost"
                            size="icon-xs"
                            aria-label={
                              bt.status === "failed"
                                ? "View failure details"
                                : "View backtest results"
                            }
                          >
                            <Link
                              href={detailHref(bt)}
                              data-testid={`backtest-detail-link-${bt.id}`}
                            >
                              <ExternalLink className="size-3.5" />
                            </Link>
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
