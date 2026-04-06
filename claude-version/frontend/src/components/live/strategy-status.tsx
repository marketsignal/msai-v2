"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Play, Square } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";
import { deployments } from "@/lib/mock-data/live-trading";
import { formatSignedCurrency, formatTimestamp } from "@/lib/format";

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

export function StrategyStatus(): React.ReactElement {
  const { getToken } = useAuth();

  const handleStartStrategy = async (deploymentId: string): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch(
        "/api/v1/live/start",
        {
          method: "POST",
          body: JSON.stringify({
            strategy_id: deploymentId,
            config: {},
            instruments: [],
            paper_trading: true,
          }),
        },
        token,
      );
    } catch (error) {
      console.error("Start strategy failed:", error);
    }
  };

  const handleStopStrategy = async (deploymentId: string): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch(
        "/api/v1/live/stop",
        {
          method: "POST",
          body: JSON.stringify({ deployment_id: deploymentId }),
        },
        token,
      );
    } catch (error) {
      console.error("Stop strategy failed:", error);
    }
  };

  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Active Deployments</CardTitle>
        <CardDescription>
          Running and stopped strategy deployments
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
              <TableHead>Strategy</TableHead>
              <TableHead>Instruments</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Start Time</TableHead>
              <TableHead className="text-right">Daily P&L</TableHead>
              <TableHead className="text-right">Total P&L</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {deployments.map((dep) => (
              <TableRow key={dep.id} className="border-border/50">
                <TableCell className="font-medium">
                  {dep.strategyName}
                </TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-1">
                    {dep.instruments.map((inst) => (
                      <Badge
                        key={inst}
                        variant="outline"
                        className="text-xs font-normal"
                      >
                        {inst}
                      </Badge>
                    ))}
                  </div>
                </TableCell>
                <TableCell>
                  <Badge
                    variant="secondary"
                    className={statusColor(dep.status)}
                  >
                    {dep.status}
                  </Badge>
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {formatTimestamp(dep.startTime)}
                </TableCell>
                <TableCell
                  className={`text-right font-medium ${
                    dep.dailyPnl >= 0 ? "text-emerald-500" : "text-red-500"
                  }`}
                >
                  {formatSignedCurrency(dep.dailyPnl)}
                </TableCell>
                <TableCell
                  className={`text-right font-medium ${
                    dep.totalPnl >= 0 ? "text-emerald-500" : "text-red-500"
                  }`}
                >
                  {formatSignedCurrency(dep.totalPnl)}
                </TableCell>
                <TableCell>
                  {dep.status === "running" ? (
                    <Button
                      variant="outline"
                      size="xs"
                      className="gap-1 text-red-400 hover:text-red-300"
                      onClick={() => handleStopStrategy(dep.id)}
                    >
                      <Square className="size-3" />
                      Stop
                    </Button>
                  ) : (
                    <Button
                      variant="outline"
                      size="xs"
                      className="gap-1"
                      onClick={() => handleStartStrategy(dep.id)}
                    >
                      <Play className="size-3" />
                      Start
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
