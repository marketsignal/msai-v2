"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  TrendingUp,
  DollarSign,
  Wifi,
  ArrowRight,
  AlertTriangle,
} from "lucide-react";
import { KillSwitch } from "@/components/live/kill-switch";
import { ResumeButton } from "@/components/live/resume-button";
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
  // Codex iter-4 P2: track token resolution separately from the value.
  // getToken() returns null in API-key-only dev mode (NEXT_PUBLIC_MSAI_API_KEY
  // fallback). The previous `if (token === null) return` guards blocked the
  // /live/status and /live/positions loads forever in that setup, hiding
  // the risk_halted banner + ResumeButton entirely.
  const [token, setToken] = useState<string | null>(null);
  const [tokenReady, setTokenReady] = useState<boolean>(false);
  const [apiError, setApiError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async (): Promise<void> => {
      const t = await getToken();
      if (!cancelled) {
        setToken(t);
        setTokenReady(true);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  // Fetch real deployments + global state from /api/v1/live/status.
  const [deployments, setDeployments] = useState<LiveDeploymentInfo[]>([]);
  const [riskHalted, setRiskHalted] = useState<boolean>(false);
  const refreshStatus = useCallback(async (): Promise<void> => {
    if (!tokenReady) return;
    try {
      const status = await getLiveStatus(token);
      setDeployments(status.deployments);
      setRiskHalted(status.risk_halted);
      setApiError(null);
    } catch {
      setApiError("Failed to load deployments");
      setDeployments([]);
    }
  }, [token, tokenReady]);

  useEffect(() => {
    if (!tokenReady) return;
    void refreshStatus();
  }, [token, tokenReady, refreshStatus]);

  // REST fallback: fetch positions when WebSocket not yet connected
  const [restPositions, setRestPositions] = useState<LivePositionItem[]>([]);
  useEffect(() => {
    if (!tokenReady) return;
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
  }, [token, tokenReady]);

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
      {/* Deploy New Portfolio entry point */}
      <Link
        data-testid="live-portfolio-deploy-link"
        href="/live-trading/portfolio"
        className="group flex items-center justify-between rounded-lg border border-border/60 bg-card/40 p-4 transition-colors hover:border-emerald-500/50 hover:bg-emerald-500/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        <div>
          <p className="text-sm font-medium text-foreground">
            Deploy New Portfolio
          </p>
          <p className="text-xs text-muted-foreground">
            Pick a portfolio revision, validate risk, and start a live
            deployment.
          </p>
        </div>
        <ArrowRight
          className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5"
          aria-hidden="true"
        />
      </Link>

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

        <div className="flex flex-col items-end gap-3">
          <div className="flex items-center gap-2">
            <ResumeButton
              riskHalted={riskHalted}
              onResumed={() => {
                void refreshStatus();
              }}
            />
            <KillSwitch
              activeCount={activeCount}
              positionCount={positionCount}
              onKilled={() => {
                void refreshStatus();
              }}
            />
          </div>
        </div>
      </div>

      {riskHalted ? (
        <div
          data-testid="risk-halted-banner"
          role="alert"
          className="flex items-start gap-2 rounded-md border border-red-500/50 bg-red-500/15 p-3 text-sm text-red-200"
        >
          <AlertTriangle
            className="mt-0.5 size-4 shrink-0 text-red-300"
            aria-hidden="true"
          />
          <span>
            <strong className="font-semibold">Trading halted.</strong> Resume
            required before new deployments can start.
          </span>
        </div>
      ) : null}

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
