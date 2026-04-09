"use client";

import { useEffect, useMemo, useState } from "react";
import { generateEquityCurve } from "@/lib/mock-data/dashboard";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { RecentTrades } from "@/components/dashboard/recent-trades";
import {
  apiGet,
  ApiError,
  getAccountSummary,
  type AccountSummary,
  type StrategyListResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

// NOTE: Account/portfolio stats and recent trades remain mocked for now —
// the backend does not yet expose dashboard-aggregated endpoints.

export default function DashboardPage(): React.ReactElement {
  const { getToken } = useAuth();
  const equityCurve = useMemo(() => generateEquityCurve(), []);
  const [strategyCount, setStrategyCount] = useState<number | undefined>(
    undefined,
  );
  const [error, setError] = useState<string | null>(null);
  const [account, setAccount] = useState<AccountSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async (): Promise<void> => {
      try {
        const token = await getToken();
        const [strategies, accountData] = await Promise.allSettled([
          apiGet<StrategyListResponse>("/api/v1/strategies/", token),
          getAccountSummary(token),
        ]);
        if (cancelled) return;
        if (strategies.status === "fulfilled") {
          setStrategyCount(strategies.value.total);
        }
        if (accountData.status === "fulfilled") {
          setAccount(accountData.value);
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
        runningStrategies={0}
        accountData={account}
      />

      {/* Equity curve + Active strategies */}
      <div className="grid gap-6 lg:grid-cols-7">
        <EquityChart data={equityCurve} />
        <ActiveStrategies />
      </div>

      {/* Recent trades */}
      <RecentTrades />
    </div>
  );
}
