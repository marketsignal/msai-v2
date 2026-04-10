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
import { positions as mockPositions } from "@/lib/mock-data/live-trading";
import { formatCurrency, formatSignedCurrency } from "@/lib/format";
import type { LivePositionItem } from "@/lib/api";

interface PositionsTableProps {
  /** Real positions from the API. Falls back to mock data if null/undefined. */
  livePositions?: LivePositionItem[] | null;
}

export function PositionsTable({
  livePositions,
}: PositionsTableProps = {}): React.ReactElement {
  // null = not yet loaded / backend unreachable → show mock
  // [] = real data, just no positions → show empty table
  const useLive = livePositions != null;

  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Open Positions</CardTitle>
        <CardDescription>
          Current open positions across all strategies
          {!useLive && (
            <span className="ml-1 text-xs text-yellow-500">(mock data)</span>
          )}
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
              <TableHead className="text-right">Unrealized P&L</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {useLive
              ? livePositions.map((pos, idx) => {
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
                })
              : mockPositions.map((pos) => (
                  <TableRow key={pos.id} className="border-border/50">
                    <TableCell className="font-medium">
                      {pos.instrument}
                    </TableCell>
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
                    <TableCell
                      className={`text-right font-medium ${
                        pos.unrealizedPnl >= 0
                          ? "text-emerald-500"
                          : "text-red-500"
                      }`}
                    >
                      {formatSignedCurrency(pos.unrealizedPnl)}
                    </TableCell>
                  </TableRow>
                ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
