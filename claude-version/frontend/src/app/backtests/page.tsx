"use client";

import { useState } from "react";
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
import { ExternalLink } from "lucide-react";
import { RunBacktestForm } from "@/components/backtests/run-form";
import { backtests } from "@/lib/mock-data/backtests";
import { formatPercent, formatDate } from "@/lib/format";

function statusColor(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export default function BacktestsPage(): React.ReactElement {
  const [runDialogOpen, setRunDialogOpen] = useState(false);

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
        <RunBacktestForm open={runDialogOpen} onOpenChange={setRunDialogOpen} />
      </div>

      {/* Backtests table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Backtest History</CardTitle>
          <CardDescription>All backtest runs across strategies</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Strategy</TableHead>
                <TableHead>Date Range</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Sharpe</TableHead>
                <TableHead className="text-right">Return</TableHead>
                <TableHead className="text-right">Trades</TableHead>
                <TableHead className="text-right">Run Date</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {backtests.map((bt) => (
                <TableRow key={bt.id} className="border-border/50">
                  <TableCell className="font-medium">
                    {bt.strategyName}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {bt.dateRange}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={statusColor(bt.status)}
                    >
                      {bt.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    {bt.status === "completed"
                      ? bt.sharpeRatio.toFixed(2)
                      : "--"}
                  </TableCell>
                  <TableCell
                    className={`text-right ${
                      bt.status === "completed"
                        ? bt.totalReturn >= 0
                          ? "text-emerald-500"
                          : "text-red-500"
                        : "text-muted-foreground"
                    }`}
                  >
                    {bt.status === "completed"
                      ? formatPercent(bt.totalReturn)
                      : "--"}
                  </TableCell>
                  <TableCell className="text-right">
                    {bt.status === "completed" ? bt.totalTrades : "--"}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    {formatDate(bt.runDate)}
                  </TableCell>
                  <TableCell>
                    {bt.status === "completed" && (
                      <Button asChild variant="ghost" size="icon-xs">
                        <Link href={`/backtests/${bt.id}`}>
                          <ExternalLink className="size-3.5" />
                        </Link>
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
