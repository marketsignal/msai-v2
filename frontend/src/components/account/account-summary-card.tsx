"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle, Wallet } from "lucide-react";
import { describeApiError, type AccountSummary } from "@/lib/api";

interface Props {
  data: AccountSummary | undefined;
  isPending: boolean;
  error: Error | null;
}

const FIELDS: { key: keyof AccountSummary; label: string; isPnl?: boolean }[] =
  [
    { key: "net_liquidation", label: "Net liquidation" },
    { key: "buying_power", label: "Buying power" },
    { key: "available_funds", label: "Available funds" },
    { key: "margin_used", label: "Margin used" },
    { key: "unrealized_pnl", label: "Unrealized P&L", isPnl: true },
    { key: "realized_pnl", label: "Realized P&L", isPnl: true },
  ];

export function AccountSummaryCard({
  data,
  isPending,
  error,
}: Props): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Wallet className="size-4 text-muted-foreground" aria-hidden="true" />
          <CardTitle className="text-base">Account summary</CardTitle>
        </div>
        <CardDescription>
          Snapshot from IBAccountSnapshot (background-refreshed every 30 s).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isPending ? (
          <SummarySkeleton />
        ) : error ? (
          <SummaryError
            message={describeApiError(error, "Failed to load account summary")}
          />
        ) : data ? (
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FIELDS.map((f) => (
              <SummaryField
                key={f.key}
                label={f.label}
                value={data[f.key]}
                isPnl={f.isPnl ?? false}
              />
            ))}
          </dl>
        ) : null}
      </CardContent>
    </Card>
  );
}

function SummaryField({
  label,
  value,
  isPnl,
}: {
  label: string;
  value: number;
  isPnl: boolean;
}): React.ReactElement {
  const formatted = `${value < 0 ? "-" : ""}$${Math.abs(value).toLocaleString(
    "en-US",
    {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    },
  )}`;
  const colorClass = isPnl
    ? value > 0
      ? "text-emerald-400"
      : value < 0
        ? "text-red-400"
        : "text-muted-foreground"
    : "text-foreground";
  return (
    <div className="space-y-1">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className={`font-mono text-lg font-semibold ${colorClass}`}>
        {formatted}
      </dd>
    </div>
  );
}

function SummarySkeleton(): React.ReactElement {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="space-y-2">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-6 w-32" />
        </div>
      ))}
    </div>
  );
}

function SummaryError({ message }: { message: string }): React.ReactElement {
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
        Failed to load summary: <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}
