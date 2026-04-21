"use client";

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
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ExternalLink } from "lucide-react";
import { RunBacktestForm } from "@/components/backtests/run-form";
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
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
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

  const load = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const token = await getToken();
      const [history, strategies] = await Promise.all([
        apiGet<BacktestHistoryResponse>("/api/v1/backtests/history", token),
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
  }, [getToken]);

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
            Run and review historical strategy backtests
          </p>
        </div>
        <RunBacktestForm
          open={runDialogOpen}
          onOpenChange={setRunDialogOpen}
          onSubmitted={() => void load()}
        />
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Backtests table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Backtest History</CardTitle>
          <CardDescription>All backtest runs across strategies</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading backtests...
            </div>
          ) : backtests.length === 0 ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              No backtests yet. Click &quot;Run Backtest&quot; to start one.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Strategy</TableHead>
                  <TableHead>Date Range</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {backtests.map((bt) => {
                  const strategy = strategiesById[bt.strategy_id];
                  return (
                    <TableRow key={bt.id} className="border-border/50">
                      <TableCell className="font-medium">
                        {strategy?.name ?? bt.strategy_id.slice(0, 8)}
                      </TableCell>
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
                              href={`/backtests/${bt.id}`}
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
