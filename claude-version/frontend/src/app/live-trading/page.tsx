"use client";

import { useState } from "react";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  OctagonX,
  Play,
  Square,
  Wifi,
  DollarSign,
  TrendingUp,
} from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";
import { deployments, positions } from "@/lib/mock-data/live-trading";
import {
  formatCurrency,
  formatSignedCurrency,
  formatTimestamp,
} from "@/lib/format";

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

export default function LiveTradingPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [killSwitchOpen, setKillSwitchOpen] = useState(false);
  const [isConnected] = useState(true);

  const handleKillAll = async (): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch("/api/v1/live/kill-all", { method: "POST" }, token);
    } catch (error) {
      console.error("Kill all failed:", error);
    }
    setKillSwitchOpen(false);
  };

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

  const totalUnrealizedPnl = positions.reduce(
    (sum, p) => sum + p.unrealizedPnl,
    0,
  );
  const totalMarketValue = positions.reduce((sum, p) => sum + p.marketValue, 0);
  const totalDailyPnl = deployments
    .filter((d) => d.status === "running")
    .reduce((sum, d) => sum + d.dailyPnl, 0);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              Live Trading
            </h1>
            <div className="flex items-center gap-1.5">
              <div
                className={`size-2 rounded-full ${
                  isConnected ? "bg-emerald-500 animate-pulse" : "bg-red-500"
                }`}
              />
              <span className="text-xs text-muted-foreground">
                {isConnected ? "Connected" : "Disconnected"}
              </span>
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            Manage live deployments and monitor open positions
          </p>
        </div>

        {/* Kill switch */}
        <Dialog open={killSwitchOpen} onOpenChange={setKillSwitchOpen}>
          <DialogTrigger asChild>
            <Button variant="destructive" className="gap-1.5">
              <OctagonX className="size-4" />
              STOP ALL
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Kill Switch - Stop All Trading</DialogTitle>
              <DialogDescription>
                This will immediately stop all running strategies, cancel all
                pending orders, and close all open positions. This action cannot
                be undone.
              </DialogDescription>
            </DialogHeader>
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
              <p className="text-sm font-medium text-red-400">
                Are you sure you want to stop all trading activity?
              </p>
              <p className="mt-1 text-xs text-red-400/80">
                {deployments.filter((d) => d.status === "running").length}{" "}
                active deployment(s) and {positions.length} open position(s)
                will be affected.
              </p>
            </div>
            <DialogFooter className="gap-2">
              <Button
                variant="outline"
                onClick={() => setKillSwitchOpen(false)}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={handleKillAll}
                className="gap-1.5"
              >
                <OctagonX className="size-4" />
                Confirm Stop All
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Daily P&L
            </CardTitle>
            <TrendingUp className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                totalDailyPnl >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatSignedCurrency(totalDailyPnl)}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Unrealized P&L
            </CardTitle>
            <DollarSign className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                totalUnrealizedPnl >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatSignedCurrency(totalUnrealizedPnl)}
            </div>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Market Value
            </CardTitle>
            <Wifi className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {formatCurrency(totalMarketValue)}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Active deployments */}
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

      {/* Positions */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Open Positions</CardTitle>
          <CardDescription>
            Current open positions across all strategies
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Avg Price</TableHead>
                <TableHead className="text-right">Current Price</TableHead>
                <TableHead className="text-right">Unrealized P&L</TableHead>
                <TableHead className="text-right">Market Value</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((pos) => (
                <TableRow key={pos.id} className="border-border/50">
                  <TableCell className="font-medium">
                    {pos.instrument}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={
                        pos.side === "LONG"
                          ? "bg-emerald-500/15 text-emerald-500"
                          : "bg-red-500/15 text-red-500"
                      }
                    >
                      {pos.side}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">{pos.quantity}</TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(pos.avgPrice)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(pos.currentPrice)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-medium ${
                      pos.unrealizedPnl >= 0
                        ? "text-emerald-500"
                        : "text-red-500"
                    }`}
                  >
                    {formatSignedCurrency(pos.unrealizedPnl)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(pos.marketValue)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
