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
import { AlertTriangle } from "lucide-react";
import type { LiveDeploymentInfo } from "@/lib/api";

function statusColor(status: string): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "bg-muted text-muted-foreground hover:bg-muted";
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

interface ActiveStrategiesProps {
  deployments: LiveDeploymentInfo[];
  /**
   * Codex iter-3 P2-A: when the parent's live-status query is in
   * ``isError``, ``deployments`` defaults to ``[]`` — indistinguishable
   * from a real "no active deployments" state. Set ``unavailable`` to
   * render an explicit degraded panel instead.
   */
  unavailable?: boolean;
}

export function ActiveStrategies({
  deployments,
  unavailable,
}: ActiveStrategiesProps): React.ReactElement {
  return (
    <Card className="border-border/50 lg:col-span-3">
      <CardHeader>
        <CardTitle className="text-base">Active Strategies</CardTitle>
        <CardDescription>Status of all deployed strategies</CardDescription>
      </CardHeader>
      <CardContent>
        {unavailable ? (
          <div
            role="status"
            className="flex h-32 items-center justify-center gap-2 text-sm text-muted-foreground"
          >
            <AlertTriangle
              className="size-4 text-amber-400"
              aria-hidden="true"
            />
            Live status unavailable — see error banner above.
          </div>
        ) : deployments.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            No active deployments.
          </div>
        ) : (
          <div className="space-y-3">
            {deployments.map((dep) => (
              <Link
                key={dep.id}
                href={`/strategies/${dep.strategy_id}`}
                className="flex items-center justify-between rounded-lg border border-border/50 p-3 transition-colors hover:bg-accent/50"
              >
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium">{dep.strategy_id}</p>
                    <Badge
                      variant="secondary"
                      className={statusColor(dep.status)}
                    >
                      {dep.status}
                    </Badge>
                  </div>
                  {dep.instruments && dep.instruments.length > 0 && (
                    <p className="text-xs text-muted-foreground">
                      {dep.instruments.join(", ")}
                    </p>
                  )}
                </div>
                {dep.paper_trading && (
                  <Badge variant="outline" className="text-xs font-normal">
                    Paper
                  </Badge>
                )}
              </Link>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
