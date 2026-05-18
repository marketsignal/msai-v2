"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Bell, AlertTriangle } from "lucide-react";
import { AlertsTable } from "@/components/alerts/alerts-table";
import { useAlerts } from "@/lib/hooks/use-alerts";
import { describeApiError } from "@/lib/api";

export default function AlertsPage(): React.ReactElement {
  const query = useAlerts(200);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Alerts</h1>
          <p className="text-sm text-muted-foreground">
            Operational alerts from the live supervisor, workers, and broker —
            most recent first.
          </p>
        </div>
        {query.dataUpdatedAt > 0 && (
          <p className="font-mono text-xs text-muted-foreground">
            Last checked{" "}
            {new Date(query.dataUpdatedAt).toLocaleTimeString("en-US", {
              hour12: false,
            })}
          </p>
        )}
      </div>

      {query.isPending ? (
        <AlertsSkeleton />
      ) : query.isError ? (
        <ErrorPanel
          message={describeApiError(query.error, "Failed to load alerts")}
        />
      ) : query.data.alerts.length === 0 ? (
        <EmptyPanel />
      ) : (
        <AlertsTable alerts={query.data.alerts} />
      )}
    </div>
  );
}

function AlertsSkeleton(): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardContent className="space-y-3 p-6" aria-busy="true">
        {[0, 1, 2, 3, 4].map((i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </CardContent>
    </Card>
  );
}

function ErrorPanel({ message }: { message: string }): React.ReactElement {
  return (
    <Card className="border-red-500/30">
      <CardContent className="flex items-start gap-3 p-6" role="alert">
        <AlertTriangle
          className="mt-0.5 size-5 shrink-0 text-red-400"
          aria-hidden="true"
        />
        <div className="space-y-1">
          <p className="text-sm font-medium text-red-400">
            Failed to load alerts
          </p>
          <p className="font-mono text-xs text-muted-foreground">{message}</p>
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyPanel(): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardContent className="flex flex-col items-center justify-center gap-3 p-12 text-center">
        <Bell className="size-8 text-muted-foreground" aria-hidden="true" />
        <div className="space-y-1">
          <p className="text-base font-medium">All quiet</p>
          <p className="text-sm text-muted-foreground">
            No recent alerts. The supervisor and workers are running normally.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
