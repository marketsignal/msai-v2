"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { AlertTriangle, FileCode, ExternalLink } from "lucide-react";
import {
  StrategyCard,
  type StrategyDeploymentStatus,
} from "@/components/strategies/strategy-card";
import {
  apiGet,
  getLiveStatus,
  describeApiError,
  type StrategyListResponse,
  type StrategyResponse,
  type LiveDeploymentInfo,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function StrategiesPage(): React.ReactElement {
  const { getToken, isAuthenticated } = useAuth();

  const strategiesQuery = useQuery<StrategyResponse[], Error>({
    queryKey: ["strategies"],
    queryFn: async (): Promise<StrategyResponse[]> => {
      const token = await getToken();
      const data = await apiGet<StrategyListResponse>(
        "/api/v1/strategies/",
        token,
      );
      return data.items;
    },
    enabled: isAuthenticated,
    staleTime: 30_000,
  });

  const liveQuery = useQuery<LiveDeploymentInfo[], Error>({
    queryKey: ["live", "status"],
    queryFn: async (): Promise<LiveDeploymentInfo[]> => {
      const token = await getToken();
      const data = await getLiveStatus(token);
      return data.deployments;
    },
    enabled: isAuthenticated,
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Strategies</h1>
        <p className="text-sm text-muted-foreground">
          Manage and monitor your trading strategies — registry-synced from{" "}
          <code className="font-mono">strategies/</code>.
        </p>
      </div>

      {strategiesQuery.isError && (
        <ListErrorPanel error={strategiesQuery.error} />
      )}

      {liveQuery.isError && !strategiesQuery.isError && (
        <Card className="border-amber-500/30">
          <CardContent
            className="flex items-start gap-2 p-3 text-sm text-amber-400"
            role="alert"
          >
            <AlertTriangle
              className="mt-0.5 size-4 shrink-0"
              aria-hidden="true"
            />
            <span>
              Live status temporarily unavailable — deployment badges may be
              stale.
            </span>
          </CardContent>
        </Card>
      )}

      {strategiesQuery.isPending ? (
        <ListSkeleton />
      ) : !strategiesQuery.data || strategiesQuery.data.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          {strategiesQuery.data.map((strategy) => (
            <StrategyCard
              key={strategy.id}
              strategy={{
                id: strategy.id,
                name: strategy.name,
                description: strategy.description,
                deploymentStatus: deriveStatus(
                  strategy.id,
                  liveQuery.data ?? [],
                ),
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function deriveStatus(
  strategyId: string,
  deployments: LiveDeploymentInfo[],
): StrategyDeploymentStatus {
  const matches = deployments.filter((d) => d.strategy_id === strategyId);
  if (matches.length === 0) return "none";
  // Codex iter-1 P2: ``starting`` / ``building`` / ``ready`` are also
  // active states (deployment is alive, just not yet trading). The prior
  // logic fell them through to "stopped" — making the UI show a deploying
  // strategy as if it were idle.
  const ACTIVE_STATES = new Set([
    "running",
    "starting",
    "building",
    "ready",
    "warming",
  ]);
  if (matches.some((d) => ACTIVE_STATES.has(d.status))) return "running";
  if (matches.some((d) => d.status === "error" || d.status === "failed"))
    return "error";
  return "stopped";
}

function ListSkeleton(): React.ReactElement {
  return (
    <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <Card key={i} className="border-border/50">
          <CardContent className="space-y-3 p-6">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="mt-6 h-10 w-full" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function EmptyState(): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardContent className="flex flex-col items-center justify-center gap-4 p-12 text-center">
        <FileCode className="size-8 text-muted-foreground" aria-hidden="true" />
        <div className="space-y-1">
          <p className="text-base font-medium">No strategies registered</p>
          <p className="max-w-md text-sm text-muted-foreground">
            Drop a Python file into{" "}
            <code className="font-mono">strategies/</code> and commit it. The
            registry sync picks it up on the next API call — no UI upload
            required (Phase&nbsp;1 architecture decision).
          </p>
        </div>
        <Button asChild variant="outline" size="sm" className="gap-2">
          <Link
            href="https://github.com/marketsignal/msai-v2"
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink className="size-3.5" aria-hidden="true" />
            How to add a strategy
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}

function ListErrorPanel({ error }: { error: Error }): React.ReactElement {
  // iter-3 describeApiError sweep.
  const msg = describeApiError(error, "Failed to load strategies.");
  return (
    <Card className="border-red-500/30">
      <CardContent className="flex items-start gap-3 p-4" role="alert">
        <AlertTriangle
          className="mt-0.5 size-5 shrink-0 text-red-400"
          aria-hidden="true"
        />
        <p className="text-sm text-red-400">{msg}</p>
      </CardContent>
    </Card>
  );
}
