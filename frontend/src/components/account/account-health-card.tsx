"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle, CheckCircle2, Plug, PlugZap } from "lucide-react";
import { describeApiError, type AccountHealth } from "@/lib/api";

interface Props {
  data: AccountHealth | undefined;
  isPending: boolean;
  error: Error | null;
}

export function AccountHealthCard({
  data,
  isPending,
  error,
}: Props): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <PlugZap
            className="size-4 text-muted-foreground"
            aria-hidden="true"
          />
          <CardTitle className="text-base">IB Gateway health</CardTitle>
        </div>
        <CardDescription>
          Cached periodic probe (gateway_connected refreshes every 30 s).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isPending ? (
          <HealthSkeleton />
        ) : error ? (
          <HealthError
            message={describeApiError(error, "Failed to load gateway health")}
          />
        ) : data ? (
          <HealthDetails data={data} />
        ) : null}
      </CardContent>
    </Card>
  );
}

function HealthDetails({ data }: { data: AccountHealth }): React.ReactElement {
  const connected = data.gateway_connected;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        {connected ? (
          <CheckCircle2
            className="size-5 text-emerald-400"
            aria-hidden="true"
          />
        ) : (
          <Plug className="size-5 text-red-400" aria-hidden="true" />
        )}
        <div>
          <p className="text-sm font-medium">
            {connected ? "Connected" : "Disconnected"}
          </p>
          <p className="text-xs text-muted-foreground">
            Status: <span className="font-mono">{data.status}</span>
          </p>
        </div>
      </div>
      <div className="flex items-baseline justify-between rounded-md border border-border/50 p-3">
        <span className="text-sm">Consecutive failures</span>
        <span
          className={`font-mono text-base font-semibold ${
            data.consecutive_failures > 0
              ? "text-red-400"
              : "text-muted-foreground"
          }`}
          data-testid="consecutive-failures"
        >
          {data.consecutive_failures}
        </span>
      </div>
    </div>
  );
}

function HealthSkeleton(): React.ReactElement {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="flex items-center gap-3">
        <Skeleton className="size-5 rounded-full" />
        <div className="space-y-2">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-24" />
        </div>
      </div>
      <Skeleton className="h-12 w-full" />
    </div>
  );
}

function HealthError({ message }: { message: string }): React.ReactElement {
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
        Failed to load gateway health:{" "}
        <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}
