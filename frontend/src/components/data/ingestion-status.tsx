"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Database, CheckCircle2, HardDrive } from "lucide-react";
import { getMarketDataStatus, type MarketDataStatusResponse } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatBytes, formatNumber } from "@/lib/format";

function statusBadgeColor(status: string): string {
  switch (status) {
    case "ok":
    case "success":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "failed":
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export function IngestionStatus(): React.ReactElement {
  const { getToken } = useAuth();
  const [data, setData] = useState<MarketDataStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const resp = await getMarketDataStatus(token);
        if (!cancelled) setData(resp);
      } catch {
        if (!cancelled) setError("Failed to load status");
      }
    };
    void load();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  return (
    <Card className="border-border/50 lg:col-span-2">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Database className="size-4 text-muted-foreground" />
          <CardTitle className="text-base">Data Status</CardTitle>
        </div>
        <CardDescription>Market data storage overview</CardDescription>
      </CardHeader>
      <CardContent>
        {error ? (
          <div className="flex h-32 items-center justify-center text-sm text-red-400">
            {error}
          </div>
        ) : !data ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            Loading...
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="size-4 text-emerald-500" />
                <span className="text-sm">Status</span>
              </div>
              <Badge
                variant="secondary"
                className={statusBadgeColor(data.status)}
              >
                {data.status}
              </Badge>
            </div>

            <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
              <div className="flex items-center gap-2">
                <HardDrive className="size-4 text-muted-foreground" />
                <span className="text-sm">Total Storage</span>
              </div>
              <span className="text-sm font-mono">
                {formatBytes(data.storage.total_bytes)}
              </span>
            </div>

            <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
              <span className="text-sm">Total Files</span>
              <span className="text-sm font-mono">
                {formatNumber(data.storage.total_files)}
              </span>
            </div>

            {Object.entries(data.storage.asset_classes).map(
              ([assetClass, bytes]) => (
                <div
                  key={assetClass}
                  className="flex items-center justify-between rounded-lg border border-border/50 p-3"
                >
                  <span className="text-sm capitalize">{assetClass}</span>
                  <span className="text-sm font-mono text-muted-foreground">
                    {formatBytes(bytes)}
                  </span>
                </div>
              ),
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
