"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Database, CheckCircle2, Clock } from "lucide-react";
import { ingestionStatus } from "@/lib/mock-data/data-management";
import { formatTimestamp, formatNumber } from "@/lib/format";

function statusBadgeColor(status: string): string {
  switch (status) {
    case "success":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "running":
      return "bg-blue-500/15 text-blue-500 hover:bg-blue-500/25";
    case "failed":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "bg-muted text-muted-foreground hover:bg-muted";
  }
}

export function IngestionStatus(): React.ReactElement {
  return (
    <Card className="border-border/50 lg:col-span-2">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Database className="size-4 text-muted-foreground" />
          <CardTitle className="text-base">Ingestion Status</CardTitle>
        </div>
        <CardDescription>Automated data ingestion pipeline</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
            <div className="flex items-center gap-2">
              <CheckCircle2 className="size-4 text-emerald-500" />
              <span className="text-sm">Status</span>
            </div>
            <Badge
              variant="secondary"
              className={statusBadgeColor(ingestionStatus.status)}
            >
              {ingestionStatus.status}
            </Badge>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
            <div className="flex items-center gap-2">
              <Clock className="size-4 text-muted-foreground" />
              <span className="text-sm">Last Run</span>
            </div>
            <span className="text-sm text-muted-foreground">
              {formatTimestamp(ingestionStatus.lastRun)}
            </span>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
            <div className="flex items-center gap-2">
              <Clock className="size-4 text-muted-foreground" />
              <span className="text-sm">Next Scheduled</span>
            </div>
            <span className="text-sm text-muted-foreground">
              {formatTimestamp(ingestionStatus.nextScheduled)}
            </span>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
            <span className="text-sm">Duration</span>
            <span className="text-sm font-mono text-muted-foreground">
              {ingestionStatus.duration}
            </span>
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border/50 p-3">
            <span className="text-sm">Records Processed</span>
            <span className="text-sm font-mono">
              {formatNumber(ingestionStatus.recordsProcessed)}
            </span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
