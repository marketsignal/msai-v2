"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Zap } from "lucide-react";
import { describeApiError, getLiveTrades, type LiveTrade } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatCurrency, formatDateTime } from "@/lib/format";

export function RecentTrades(): React.ReactElement {
  const { getToken } = useAuth();
  const [trades, setTrades] = useState<LiveTrade[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const data = await getLiveTrades(token);
        if (!cancelled) setTrades(data.trades.slice(0, 10));
      } catch (err) {
        // iter-4 SF P2: bare catch was the same silent-failure pattern
        // the rest of the sweep eliminated. surface backend
        // HTTPException detail (e.g. "ib_gateway_unreachable", 401
        // token-expired) via describeApiError.
        if (!cancelled)
          setError(describeApiError(err, "Failed to load trades"));
      }
    };
    void load();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Zap className="size-4 text-muted-foreground" />
          <CardTitle className="text-base">Recent Trades</CardTitle>
        </div>
        <CardDescription>Last 10 executed trades</CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <div className="mb-3 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-400">
            {error}
          </div>
        )}
        {trades.length === 0 && !error ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            No recent trades yet.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((trade) => (
                <TableRow key={trade.id} className="border-border/50">
                  <TableCell className="text-muted-foreground">
                    {formatDateTime(trade.timestamp)}
                  </TableCell>
                  <TableCell className="font-medium">
                    {trade.instrument_id}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={
                        trade.side === "BUY"
                          ? "bg-emerald-500/15 text-emerald-500"
                          : "bg-red-500/15 text-red-500"
                      }
                    >
                      {trade.side}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">{trade.quantity}</TableCell>
                  <TableCell className="text-right">
                    {trade.price
                      ? formatCurrency(parseFloat(trade.price))
                      : "--"}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {trade.status}
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
