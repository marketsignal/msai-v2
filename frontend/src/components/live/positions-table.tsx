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
import { formatCurrency, formatSignedCurrency } from "@/lib/format";
import type { LivePositionItem } from "@/lib/api";

interface PositionsTableProps {
  /** Real positions from the API. */
  livePositions?: LivePositionItem[];
}

export function PositionsTable({
  livePositions = [],
}: PositionsTableProps): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Open Positions</CardTitle>
        <CardDescription>
          Current open positions across all strategies
        </CardDescription>
      </CardHeader>
      <CardContent>
        {livePositions.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            No open positions.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Avg Price</TableHead>
                <TableHead className="text-right">Unrealized P&L</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {livePositions.map((pos, idx) => {
                const qty = parseFloat(pos.qty);
                const pnl = parseFloat(pos.unrealized_pnl);
                return (
                  <TableRow
                    key={`${pos.instrument_id}-${idx}`}
                    className="border-border/50"
                  >
                    <TableCell className="font-medium">
                      {pos.instrument_id}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={
                          qty >= 0
                            ? "bg-emerald-500/15 text-emerald-500"
                            : "bg-red-500/15 text-red-500"
                        }
                      >
                        {qty >= 0 ? "LONG" : "SHORT"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {Math.abs(qty)}
                    </TableCell>
                    <TableCell className="text-right">
                      {formatCurrency(parseFloat(pos.avg_price))}
                    </TableCell>
                    <TableCell
                      className={`text-right font-medium ${
                        pnl >= 0 ? "text-emerald-500" : "text-red-500"
                      }`}
                    >
                      {formatSignedCurrency(pnl)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
