"use client";

import { useQuery } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Database, AlertTriangle } from "lucide-react";
import {
  describeApiError,
  getMarketDataStatus,
  type MarketDataStatusResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * StorageStatsCard — market-data Parquet storage health (T18, M-6).
 *
 * Reads cached storage stats from GET /api/v1/market-data/status.
 * Polls every 60 s — these numbers move slowly (asset class breakdown
 * + total bytes), no need for aggressive cadence.
 */
export function StorageStatsCard(): React.ReactElement {
  const { getToken, isAuthenticated } = useAuth();
  const query = useQuery<MarketDataStatusResponse, Error>({
    queryKey: ["market-data", "status"],
    queryFn: async (): Promise<MarketDataStatusResponse> => {
      const token = await getToken();
      return getMarketDataStatus(token);
    },
    enabled: isAuthenticated,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1,
  });

  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Database
            className="size-4 text-muted-foreground"
            aria-hidden="true"
          />
          <CardTitle className="text-base">Market-data storage</CardTitle>
        </div>
        <CardDescription>
          Parquet bytes on disk by asset class — live from{" "}
          <code className="font-mono">/api/v1/market-data/status</code>.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <StorageSkeleton />
        ) : query.isError ? (
          <StorageError
            message={describeApiError(
              query.error,
              "Failed to load storage stats",
            )}
          />
        ) : query.data ? (
          <StorageDetails data={query.data} />
        ) : null}
      </CardContent>
    </Card>
  );
}

function StorageDetails({
  data,
}: {
  data: MarketDataStatusResponse;
}): React.ReactElement {
  const totals = data.storage;
  const assetClasses = Object.entries(totals.asset_classes);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <Stat
          label="Total files"
          value={totals.total_files.toLocaleString("en-US")}
        />
        <Stat label="Total bytes" value={formatBytes(totals.total_bytes)} />
      </div>
      {assetClasses.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-wide text-muted-foreground">
            Files by asset class
          </p>
          <div
            className="flex flex-wrap gap-2"
            data-testid="storage-asset-classes"
          >
            {assetClasses.map(([cls, count]) => (
              <Badge
                key={cls}
                variant="secondary"
                className="bg-muted/60 text-muted-foreground"
              >
                <span className="font-mono">{cls}</span>
                <span className="ml-2 font-mono text-foreground">{count}</span>
              </Badge>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
}: {
  label: string;
  value: string;
}): React.ReactElement {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="font-mono text-lg font-semibold">{value}</p>
    </div>
  );
}

function StorageSkeleton(): React.ReactElement {
  return (
    <div className="space-y-4" aria-busy="true">
      <div className="grid gap-4 sm:grid-cols-2">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
      <Skeleton className="h-8 w-3/4" />
    </div>
  );
}

function StorageError({ message }: { message: string }): React.ReactElement {
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
        Failed to load storage stats:{" "}
        <span className="font-mono">{message}</span>
      </div>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
