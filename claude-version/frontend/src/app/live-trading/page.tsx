"use client";

import { useEffect, useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TrendingUp, DollarSign, Wifi } from "lucide-react";
import { KillSwitch } from "@/components/live/kill-switch";
import { StrategyStatus } from "@/components/live/strategy-status";
import { PositionsTable } from "@/components/live/positions-table";
import {
  getLivePositions,
  getLiveStatus,
  type LivePositionItem,
  type LiveDeploymentInfo,
} from "@/lib/api";
import { formatCurrency, formatSignedCurrency } from "@/lib/format";
import { useAuth } from "@/lib/auth";
import { useLiveStream } from "@/lib/use-live-stream";

export default function LiveTradingPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [token, setToken] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async (): Promise<void> => {
      const t = await getToken();
      if (!cancelled) {
        setToken(t);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  // Fetch real deployments from /api/v1/live/status.
  const [deployments, setDeployments] = useState<LiveDeploymentInfo[]>([]);
  useEffect(() => {
    if (token === null) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const status = await getLiveStatus(token);
        if (!cancelled) {
          setDeployments(status.deployments);
        }
      } catch {
        if (!cancelled) {
          setApiError("Failed to load deployments");
          setDeployments([]);
        }
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token]);

  // REST fallback: fetch positions when WebSocket not yet connected
  const [restPositions, setRestPositions] = useState<LivePositionItem[]>([]);
  useEffect(() => {
    if (token === null) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const data = await getLivePositions(token);
        if (!cancelled) setRestPositions(data.positions);
      } catch {
        // Backend unreachable — leave empty
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token]);

  const activeRealDeployment = deployments.find((d) => d.status === "running");
  const live = useLiveStream(activeRealDeployment?.id ?? null, { token });

  const isConnected = live.connectionState === "open";
  const usingLive = isConnected;
  const livePositions = live.positions;

  // Positions for the table: WebSocket > REST > empty
  const positionsForTable = usingLive ? livePositions : restPositions;

  const totalUnrealizedPnl = useMemo(() => {
    return positionsForTable.reduce(
      (sum, p) => sum + parseFloat(p.unrealized_pnl),
      0,
    );
  }, [positionsForTable]);

  const totalMarketValue = useMemo(() => {
    return positionsForTable.reduce(
      (sum, p) => sum + parseFloat(p.qty) * parseFloat(p.avg_price),
      0,
    );
  }, [positionsForTable]);

  const totalDailyPnl = useMemo(() => {
    if (usingLive) {
      return livePositions.reduce(
        (sum, p) => sum + parseFloat(p.realized_pnl),
        0,
      );
    }
    return 0;
  }, [usingLive, livePositions]);

  const activeCount = deployments.filter((d) => d.status === "running").length;
  const positionCount = positionsForTable.length;

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

        <KillSwitch activeCount={activeCount} positionCount={positionCount} />
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

      {apiError && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {apiError}
        </div>
      )}

      <StrategyStatus deployments={deployments} />
      <PositionsTable livePositions={positionsForTable} />
    </div>
  );
}
