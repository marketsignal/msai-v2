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
}

export function ActiveStrategies({
  deployments,
}: ActiveStrategiesProps): React.ReactElement {
  return (
    <Card className="border-border/50 lg:col-span-3">
      <CardHeader>
        <CardTitle className="text-base">Active Strategies</CardTitle>
        <CardDescription>Status of all deployed strategies</CardDescription>
      </CardHeader>
      <CardContent>
        {deployments.length === 0 ? (
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
