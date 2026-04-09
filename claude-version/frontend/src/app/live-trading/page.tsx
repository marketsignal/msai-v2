"use client";

import { useEffect, useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TrendingUp, DollarSign, Wifi } from "lucide-react";
import { KillSwitch } from "@/components/live/kill-switch";
import { StrategyStatus } from "@/components/live/strategy-status";
import { PositionsTable } from "@/components/live/positions-table";
import {
  deployments as mockDeployments,
  positions,
} from "@/lib/mock-data/live-trading";
import { getLivePositions, type LivePositionItem } from "@/lib/api";
import { formatCurrency, formatSignedCurrency } from "@/lib/format";
import { getLiveStatus, type LiveDeploymentInfo } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useLiveStream } from "@/lib/use-live-stream";

export default function LiveTradingPage(): React.ReactElement {
  // Phase 3 task 3.10: subscribe to the live event stream for
  // the FIRST running deployment (Phase 3 ships single-deployment;
  // multi-deployment broadcast comes in Phase 5). When no
  // deployment is active OR the WebSocket is not connected
  // yet, the page renders the mock data so designers can
  // preview the layout offline.
  //
  // Codex batch 9 P1 iter 2: the hook refuses to connect in
  // production without a JWT. Get one from MSAL via the
  // useAuth context and pass it to the hook. In dev mode the
  // hook falls back to NEXT_PUBLIC_MSAI_API_KEY automatically.
  const { getToken } = useAuth();
  const [token, setToken] = useState<string | null>(null);
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

  // Codex batch 9 P1 iter 3: fetch real deployments from
  // /api/v1/live/status. Mock data is only used when the
  // backend is unreachable (designers previewing the page
  // without the stack running). The hook subscribes to the
  // first RUNNING real deployment, never to a mock id.
  const [realDeployments, setRealDeployments] = useState<
    LiveDeploymentInfo[] | null
  >(null);
  useEffect(() => {
    if (token === null) {
      // Wait for the token to resolve before hitting the API
      return;
    }
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const status = await getLiveStatus(token);
        if (!cancelled) {
          setRealDeployments(status.deployments);
        }
      } catch {
        // Backend unreachable — leave realDeployments as null
        // so the page falls back to mock data for the offline
        // designer preview path.
        if (!cancelled) {
          setRealDeployments(null);
        }
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token]);

  // REST fallback: fetch positions when WebSocket not yet connected
  const [restPositions, setRestPositions] = useState<LivePositionItem[] | null>(
    null,
  );
  useEffect(() => {
    if (token === null) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const data = await getLivePositions(token);
        if (!cancelled) setRestPositions(data.positions);
      } catch {
        // Backend unreachable — leave null
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token]);

  const activeRealDeployment = realDeployments?.find(
    (d) => d.status === "running",
  );
  const live = useLiveStream(activeRealDeployment?.id ?? null, { token });
  const deployments = realDeployments ?? mockDeployments;

  const isConnected = live.connectionState === "open";

  // Codex batch 9 P2: select the source ONCE based on the
  // connection state — NOT based on whether the live data
  // happens to be non-empty. An empty real deployment must
  // render as empty, not silently fall back to mock PnL.
  const usingLive = isConnected;
  const livePositions = live.positions;

  // Positions for the table: WebSocket > REST > null (mock fallback in component)
  const positionsForTable = usingLive ? livePositions : restPositions;

  const totalUnrealizedPnl = useMemo(() => {
    if (usingLive) {
      return livePositions.reduce(
        (sum, p) => sum + parseFloat(p.unrealized_pnl),
        0,
      );
    }
    return positions.reduce((sum, p) => sum + p.unrealizedPnl, 0);
  }, [usingLive, livePositions]);

  const totalMarketValue = useMemo(() => {
    if (usingLive) {
      return livePositions.reduce(
        (sum, p) => sum + parseFloat(p.qty) * parseFloat(p.avg_price),
        0,
      );
    }
    return positions.reduce((sum, p) => sum + p.marketValue, 0);
  }, [usingLive, livePositions]);

  const totalDailyPnl = useMemo(() => {
    if (usingLive) {
      return livePositions.reduce(
        (sum, p) => sum + parseFloat(p.realized_pnl),
        0,
      );
    }
    // Mock-data path only — real deployments don't carry a
    // dailyPnl field in the /live/status response; the value
    // comes from the WebSocket fast path above. When the
    // backend is reachable (realDeployments != null) but
    // not yet streaming, we show zero.
    if (realDeployments !== null) {
      return 0;
    }
    return mockDeployments
      .filter((d) => d.status === "running")
      .reduce((sum, d) => sum + d.dailyPnl, 0);
  }, [usingLive, livePositions, realDeployments]);

  const activeCount = deployments.filter((d) => d.status === "running").length;
  const positionCount = usingLive ? livePositions.length : positions.length;

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

      <StrategyStatus />
      <PositionsTable livePositions={positionsForTable} />
    </div>
  );
}
