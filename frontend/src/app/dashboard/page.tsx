"use client";

import { useEffect, useState } from "react";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { RecentTrades } from "@/components/dashboard/recent-trades";
import {
  apiGet,
  ApiError,
  getAccountSummary,
  getLiveStatus,
  type AccountSummary,
  type StrategyListResponse,
  type LiveDeploymentInfo,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function DashboardPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [strategyCount, setStrategyCount] = useState<number | undefined>(
    undefined,
  );
  const [runningCount, setRunningCount] = useState<number>(0);
  const [deployments, setDeployments] = useState<LiveDeploymentInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [account, setAccount] = useState<AccountSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const [strategies, accountData, liveStatus] = await Promise.allSettled([
          apiGet<StrategyListResponse>("/api/v1/strategies/", token),
          getAccountSummary(token),
          getLiveStatus(token),
        ]);
        if (cancelled) return;
        if (strategies.status === "fulfilled") {
          setStrategyCount(strategies.value.total);
        } else {
          setError("Failed to load strategies");
        }
        if (accountData.status === "fulfilled") {
          setAccount(accountData.value);
        }
        if (liveStatus.status === "fulfilled") {
          setDeployments(liveStatus.value.deployments);
          setRunningCount(
            liveStatus.value.deployments.filter((d) => d.status === "running")
              .length,
          );
        }
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `Failed to load data (${err.status})`
            : "Failed to load data";
        setError(msg);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [getToken]);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Overview of your trading performance and market signals
        </p>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Stats grid */}
      <PortfolioSummary
        totalStrategies={strategyCount}
        runningStrategies={runningCount}
        accountData={account}
      />

      {/* Equity curve + Active strategies */}
      <div className="grid gap-6 lg:grid-cols-7">
        <EquityChart data={[]} />
        <ActiveStrategies deployments={deployments} />
      </div>

      {/* Recent trades */}
      <RecentTrades />
    </div>
  );
}
