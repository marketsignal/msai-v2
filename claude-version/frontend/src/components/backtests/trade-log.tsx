"use client";

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
import type { BacktestTradeItem } from "@/lib/api";
import {
  formatCurrency,
  formatSignedCurrency,
  formatDateTime,
} from "@/lib/format";

interface TradeLogProps {
  trades: BacktestTradeItem[];
}

export function TradeLog({ trades }: TradeLogProps): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Trade Log</CardTitle>
        <CardDescription>
          Individual trades executed during the backtest
        </CardDescription>
      </CardHeader>
      <CardContent>
        {trades.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            No trade data available yet.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">Exit</TableHead>
                <TableHead className="text-right">P&L</TableHead>
                <TableHead className="text-right">Duration</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {trades.map((trade) => (
                <TableRow key={trade.id} className="border-border/50">
                  <TableCell className="text-muted-foreground">
                    {formatDateTime(trade.timestamp)}
                  </TableCell>
                  <TableCell className="font-medium">
                    {trade.instrument}
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
                    {formatCurrency(trade.entryPrice)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(trade.exitPrice)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-medium ${
                      trade.pnl >= 0 ? "text-emerald-500" : "text-red-500"
                    }`}
                  >
                    {formatSignedCurrency(trade.pnl)}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    {trade.holdingPeriod}
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
