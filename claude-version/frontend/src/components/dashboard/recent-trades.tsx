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
import { Zap } from "lucide-react";
import { recentTrades } from "@/lib/mock-data/dashboard";
import {
  formatCurrency,
  formatSignedCurrency,
  formatDateTime,
} from "@/lib/format";

export function RecentTrades(): React.ReactElement {
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
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
              <TableHead>Timestamp</TableHead>
              <TableHead>Instrument</TableHead>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Price</TableHead>
              <TableHead className="text-right">P&L</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {recentTrades.map((trade) => (
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
                  {formatCurrency(trade.price)}
                </TableCell>
                <TableCell
                  className={`text-right font-medium ${
                    trade.pnl >= 0 ? "text-emerald-500" : "text-red-500"
                  }`}
                >
                  {formatSignedCurrency(trade.pnl)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
