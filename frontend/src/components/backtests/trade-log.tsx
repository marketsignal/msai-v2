"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { ChevronLeft, ChevronRight, Loader2 } from "lucide-react";
import {
  ApiError,
  getBacktestTrades,
  type BacktestTradeItem,
  type BacktestAccounting,
  type BacktestTradesResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";

/** User-facing copy for a failed ``/trades`` fetch. */
function apiErrorToTradesCopy(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { error?: { code?: string } } | undefined;
    if (body?.error?.code === "NOT_FOUND") {
      return "This backtest no longer exists.";
    }
    return `Unable to load trades (HTTP ${e.status}).`;
  }
  return e instanceof Error ? e.message : "Failed to load trades.";
}

interface TradeLogProps {
  backtestId: string;
  /** Matches backend default; server clamps at 500. */
  pageSize?: number;
}

/**
 * Paginated log of individual Nautilus fills for a backtest.
 *
 * Each row is ONE fill (not an entry/exit pair) — the previous round-trip
 * shape was a frontend-only fabrication that didn't align with the backend
 * ``Trade`` table. Fetches from ``GET /api/v1/backtests/{id}/trades`` in
 * pages of ``pageSize`` (default 100). Server-side secondary sort on
 * ``Trade.id`` makes pagination stable even when multiple fills share the
 * same ``executed_at``.
 */
export function TradeLog({
  backtestId,
  pageSize = 100,
}: TradeLogProps): React.ReactElement {
  const { getToken } = useAuth();
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<BacktestTradeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [accounting, setAccounting] = useState<BacktestAccounting | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      setLoading(true);
      setError(null);
      try {
        const token = await getToken();
        const res: BacktestTradesResponse = await getBacktestTrades(
          backtestId,
          { page, page_size: pageSize },
          token,
        );
        if (cancelled) return;
        setItems(res.items);
        setTotal(res.total);
        setAccounting(res.accounting ?? null);
      } catch (e: unknown) {
        if (!cancelled) {
          setError(apiErrorToTradesCopy(e));
          setItems([]);
          setTotal(0);
          setAccounting(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [backtestId, page, pageSize, getToken, reloadCount]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const canPrev = page > 1 && !loading && !error;
  const canNext = page < totalPages && !loading && !error;

  return (
    <Card className="border-border/50" data-testid="trade-log">
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="text-base">
            {loading || error
              ? "Execution Records"
              : accounting ? "Fill Log" : "Legacy Order Records"}
          </CardTitle>
          <CardDescription>
            {loading ? "Loading execution records…" : error ? "Record count unavailable." : (
              <>{total} {accounting ? "fills" : "records"} · Page {page} of {totalPages}</>
            )}
            {!loading && !error && !accounting && (
              <span className="block mt-1">
                Recorded order quantities and prices are unverified.
                P&amp;L and fees are unavailable.
              </span>
            )}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={!canPrev}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((p) => p + 1)}
            disabled={!canNext}
            aria-label="Next page"
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div
            className="flex items-center justify-center py-8"
            role="status"
            aria-label="Loading trades"
          >
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : error !== null ? (
          <div className="flex flex-col items-center gap-3 py-8" role="alert">
            <p className="text-center text-sm text-destructive">
              Unable to load trades: {error}
            </p>
            <Button variant="outline" onClick={() => setReloadCount((count) => count + 1)}>
              Retry
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No {accounting ? "fills" : "records"} in this backtest.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">
                  {accounting ? "Filled quantity" : "Reported quantity"}
                </TableHead>
                <TableHead className="text-right">
                  {accounting ? "Fill price (USD)" : "Reported price"}
                </TableHead>
                <TableHead className="text-right">Realized P&amp;L (USD)</TableHead>
                <TableHead className="text-right">Commission (USD)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((t) => (
                <TableRow key={t.id} data-testid={`trade-row-${t.id}`}>
                  <TableCell className="font-mono text-xs">
                    {formatDateTime(t.executed_at)}
                  </TableCell>
                  <TableCell>{t.instrument}</TableCell>
                  <TableCell>
                    <Badge variant={t.side === "BUY" ? "default" : "secondary"}>
                      {t.side}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">{t.quantity}</TableCell>
                  <TableCell className="text-right">
                    {accounting ? "$" : ""}{t.price.toFixed(2)}
                  </TableCell>
                  <TableCell
                    className={`text-right ${
                      t.pnl === null
                        ? "text-muted-foreground"
                        : t.pnl >= 0 ? "text-emerald-500" : "text-red-500"
                    }`}
                  >
                    {t.pnl === null ? "Unavailable" : `$${t.pnl.toFixed(2)}`}
                  </TableCell>
                  <TableCell className="text-right">
                    {t.commission === null ? "Unavailable" : `$${t.commission.toFixed(2)}`}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
