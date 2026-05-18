"use client";

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
import { Skeleton } from "@/components/ui/skeleton";
import { Bell, AlertTriangle, ChevronRight } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/lib/auth";
import { describeApiError, getAlerts, type AlertRecord } from "@/lib/api";

/**
 * AlertsFeed — dashboard sidecar showing the N most-recent alerts.
 *
 * Replaces the permanently-empty EquityChart slot per Revision R-iter
 * (audit finding F-11). Polls /api/v1/alerts/ every 60 s per the
 * research-validated polling cheat-sheet.
 */
export function AlertsFeed({
  limit = 5,
}: { limit?: number } = {}): React.ReactElement {
  const { getToken } = useAuth();
  const query = useQuery<AlertRecord[], Error>({
    queryKey: ["alerts", "feed", limit],
    queryFn: async (): Promise<AlertRecord[]> => {
      const token = await getToken();
      const data = await getAlerts(token, limit);
      return data.alerts;
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  return (
    <Card className="border-border/50 lg:col-span-4">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Bell className="size-4 text-muted-foreground" aria-hidden="true" />
            <CardTitle className="text-base">Recent alerts</CardTitle>
          </div>
          <Button asChild variant="ghost" size="sm" className="gap-1">
            <Link href="/alerts">
              View all
              <ChevronRight className="size-3.5" aria-hidden="true" />
            </Link>
          </Button>
        </div>
        <CardDescription>
          Operational alerts from the live supervisor, workers, and broker.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <FeedSkeleton />
        ) : query.isError ? (
          <FeedError
            message={describeApiError(query.error, "Failed to load alerts")}
          />
        ) : query.data.length === 0 ? (
          <EmptyState />
        ) : (
          <ul className="space-y-2" data-testid="alerts-feed-list">
            {query.data.map((alert, idx) => (
              <li key={`${alert.created_at}-${idx}`}>
                <AlertRow alert={alert} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function AlertRow({ alert }: { alert: AlertRecord }): React.ReactElement {
  return (
    <Link
      href="/alerts"
      className="flex items-start gap-3 rounded-md border border-border/50 p-3 transition-colors hover:bg-accent/50"
      data-testid="alerts-feed-row"
    >
      <LevelIndicator level={alert.level} />
      <div className="flex-1 space-y-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-sm font-medium">{alert.title}</p>
          <time className="shrink-0 font-mono text-xs text-muted-foreground">
            {formatRelative(alert.created_at)}
          </time>
        </div>
        <p className="line-clamp-2 text-xs text-muted-foreground">
          {alert.message}
        </p>
      </div>
    </Link>
  );
}

function LevelIndicator({ level }: { level: string }): React.ReactElement {
  const variant = levelVariant(level);
  return (
    <Badge variant="secondary" className={`gap-1 ${variant.className}`}>
      {variant.icon}
      {level}
    </Badge>
  );
}

function levelVariant(level: string): {
  className: string;
  icon: React.ReactElement;
} {
  const normalized = level.toLowerCase();
  if (normalized === "error" || normalized === "critical") {
    return {
      className: "bg-red-500/15 text-red-400",
      icon: <AlertTriangle className="size-3" aria-hidden="true" />,
    };
  }
  if (normalized === "warning") {
    return {
      className: "bg-amber-500/15 text-amber-400",
      icon: <AlertTriangle className="size-3" aria-hidden="true" />,
    };
  }
  return {
    className: "bg-muted text-muted-foreground",
    icon: <Bell className="size-3" aria-hidden="true" />,
  };
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function FeedSkeleton(): React.ReactElement {
  return (
    <ul className="space-y-2" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <li
          key={i}
          className="flex items-start gap-3 rounded-md border border-border/50 p-3"
        >
          <Skeleton className="size-6 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-full" />
          </div>
        </li>
      ))}
    </ul>
  );
}

function FeedError({ message }: { message: string }): React.ReactElement {
  return (
    <div
      className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
      role="alert"
    >
      Failed to load alerts: <span className="font-mono">{message}</span>
    </div>
  );
}

function EmptyState(): React.ReactElement {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
      <Bell className="size-6 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm text-muted-foreground">
        All quiet — no recent alerts.
      </p>
    </div>
  );
}
