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
import { positions } from "@/lib/mock-data/live-trading";
import { formatCurrency, formatSignedCurrency } from "@/lib/format";

export function PositionsTable(): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Open Positions</CardTitle>
        <CardDescription>
          Current open positions across all strategies
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
              <TableHead>Instrument</TableHead>
              <TableHead>Side</TableHead>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Avg Price</TableHead>
              <TableHead className="text-right">Current Price</TableHead>
              <TableHead className="text-right">Unrealized P&L</TableHead>
              <TableHead className="text-right">Market Value</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {positions.map((pos) => (
              <TableRow key={pos.id} className="border-border/50">
                <TableCell className="font-medium">{pos.instrument}</TableCell>
                <TableCell>
                  <Badge
                    variant="secondary"
                    className={
                      pos.side === "LONG"
                        ? "bg-emerald-500/15 text-emerald-500"
                        : "bg-red-500/15 text-red-500"
                    }
                  >
                    {pos.side}
                  </Badge>
                </TableCell>
                <TableCell className="text-right">{pos.quantity}</TableCell>
                <TableCell className="text-right">
                  {formatCurrency(pos.avgPrice)}
                </TableCell>
                <TableCell className="text-right">
                  {formatCurrency(pos.currentPrice)}
                </TableCell>
                <TableCell
                  className={`text-right font-medium ${
                    pos.unrealizedPnl >= 0 ? "text-emerald-500" : "text-red-500"
                  }`}
                >
                  {formatSignedCurrency(pos.unrealizedPnl)}
                </TableCell>
                <TableCell className="text-right">
                  {formatCurrency(pos.marketValue)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
