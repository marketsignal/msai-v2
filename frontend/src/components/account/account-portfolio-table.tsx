"use client";

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
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle, PieChart } from "lucide-react";
import { describeApiError, type AccountPortfolioItem } from "@/lib/api";

interface Props {
  data: AccountPortfolioItem[] | undefined;
  isPending: boolean;
  error: Error | null;
}

export function AccountPortfolioTable({
  data,
  isPending,
  error,
}: Props): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <PieChart
            className="size-4 text-muted-foreground"
            aria-hidden="true"
          />
          <CardTitle className="text-base">Broker positions</CardTitle>
        </div>
        <CardDescription>
          Open positions from the IB account, snapshot-cached.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isPending ? (
          <PortfolioSkeleton />
        ) : error ? (
          <PortfolioError
            message={describeApiError(error, "Failed to load portfolio")}
          />
        ) : !data || data.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="overflow-hidden rounded-md border border-border/50">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead className="text-right">Position</TableHead>
                  <TableHead className="text-right">Avg cost</TableHead>
                  <TableHead className="text-right">Market value</TableHead>
                  <TableHead className="text-right">Unrealized P&L</TableHead>
                  <TableHead className="text-right">Realized P&L</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((row, idx) => (
                  <TableRow key={`${row.symbol}-${idx}`}>
                    <TableCell className="font-mono font-medium">
                      {row.symbol}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {row.position.toLocaleString("en-US")}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatMoney(row.average_cost)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatMoney(row.market_value)}
                    </TableCell>
                    <TableCell
                      className={`text-right font-mono ${pnlColor(row.unrealized_pnl)}`}
                    >
                      {formatPnl(row.unrealized_pnl)}
                    </TableCell>
                    <TableCell
                      className={`text-right font-mono ${pnlColor(row.realized_pnl)}`}
                    >
                      {formatPnl(row.realized_pnl)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PortfolioSkeleton(): React.ReactElement {
  return (
    <div className="space-y-2" aria-busy="true">
      {Array.from({ length: 4 }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

function PortfolioError({ message }: { message: string }): React.ReactElement {
  return (
    <div
      className="flex items-start gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-3"
      role="alert"
    >
      <AlertTriangle
        className="mt-0.5 size-4 shrink-0 text-red-400"
        aria-hidden="true"
      />
      <div className="text-sm text-red-400">
        Failed to load positions: <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}

function EmptyState(): React.ReactElement {
  return (
    <div className="flex flex-col items-center gap-2 py-8 text-center">
      <PieChart className="size-6 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm text-muted-foreground">
        No open positions in the IB account.
      </p>
    </div>
  );
}

function formatMoney(value: number | undefined): string {
  if (value === undefined) return "—";
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatPnl(value: number | undefined): string {
  if (value === undefined) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function pnlColor(value: number | undefined): string {
  if (value === undefined || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-emerald-400" : "text-red-400";
}
