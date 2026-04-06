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
import { activeStrategies } from "@/lib/mock-data/dashboard";
import { formatSignedCurrency } from "@/lib/format";

function statusColor(status: "running" | "stopped" | "error"): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "bg-muted text-muted-foreground hover:bg-muted";
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
  }
}

export function ActiveStrategies(): React.ReactElement {
  return (
    <Card className="border-border/50 lg:col-span-3">
      <CardHeader>
        <CardTitle className="text-base">Active Strategies</CardTitle>
        <CardDescription>Status of all deployed strategies</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {activeStrategies.map((strategy) => (
            <Link
              key={strategy.id}
              href={`/strategies/${strategy.id}`}
              className="flex items-center justify-between rounded-lg border border-border/50 p-3 transition-colors hover:bg-accent/50"
            >
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium">{strategy.name}</p>
                  <Badge
                    variant="secondary"
                    className={statusColor(strategy.status)}
                  >
                    {strategy.status}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  {strategy.instruments.join(", ")}
                </p>
              </div>
              {strategy.status === "running" && (
                <span
                  className={`text-sm font-medium ${
                    strategy.dailyPnl >= 0 ? "text-emerald-500" : "text-red-500"
                  }`}
                >
                  {formatSignedCurrency(strategy.dailyPnl)}
                </span>
              )}
            </Link>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
