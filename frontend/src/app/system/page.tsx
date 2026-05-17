"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle } from "lucide-react";
import { SubsystemRow } from "@/components/system/subsystem-row";
import { VersionInfoCard } from "@/components/system/version-info-card";
import { useSystemHealth } from "@/lib/hooks/use-system-health";
import { describeApiError } from "@/lib/api";

export default function SystemPage(): React.ReactElement {
  const query = useSystemHealth();

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            System health
          </h1>
          <p className="text-sm text-muted-foreground">
            Real subsystem statuses + build metadata. Polls every 30 s.
          </p>
        </div>
        {query.dataUpdatedAt > 0 && (
          <p className="font-mono text-xs text-muted-foreground">
            Last refresh{" "}
            {new Date(query.dataUpdatedAt).toLocaleTimeString("en-US", {
              hour12: false,
            })}
          </p>
        )}
      </div>

      {query.isPending ? (
        <SystemSkeleton />
      ) : query.isError ? (
        <SystemError
          message={describeApiError(
            query.error,
            "Failed to load system health",
          )}
        />
      ) : query.data ? (
        <>
          <VersionInfoCard
            version={query.data.version}
            commitSha={query.data.commit_sha}
            uptimeSeconds={query.data.uptime_seconds}
          />

          <Card className="border-border/50">
            <CardContent className="space-y-3 p-6">
              {Object.entries(query.data.subsystems).map(([name, status]) => (
                <SubsystemRow key={name} name={name} status={status} />
              ))}
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function SystemSkeleton(): React.ReactElement {
  return (
    <>
      <Card className="border-border/50">
        <CardContent className="grid gap-4 p-6 sm:grid-cols-3" aria-busy="true">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-6 w-28" />
            </div>
          ))}
        </CardContent>
      </Card>
      <Card className="border-border/50">
        <CardContent className="space-y-3 p-6" aria-busy="true">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </CardContent>
      </Card>
    </>
  );
}

function SystemError({ message }: { message: string }): React.ReactElement {
  return (
    <Card className="border-red-500/30">
      <CardContent className="flex items-start gap-3 p-6" role="alert">
        <AlertTriangle
          className="mt-0.5 size-5 shrink-0 text-red-400"
          aria-hidden="true"
        />
        <div className="space-y-1">
          <p className="text-sm font-medium text-red-400">
            Failed to load system health
          </p>
          <p className="font-mono text-xs text-muted-foreground">{message}</p>
        </div>
      </CardContent>
    </Card>
  );
}
