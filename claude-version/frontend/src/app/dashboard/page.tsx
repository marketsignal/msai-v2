"use client";

import { useMemo } from "react";
import { generateEquityCurve } from "@/lib/mock-data/dashboard";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { RecentTrades } from "@/components/dashboard/recent-trades";

// TODO: Replace mock data with API calls when backend is running:
// import { useAuth } from "@/lib/auth";
// import { apiFetch } from "@/lib/api";
// const { getToken } = useAuth();
// const token = await getToken();
// const summary = await apiFetch("/api/v1/account/summary", {}, token);
// const strategies = await apiFetch("/api/v1/live/status", {}, token);
// const trades = await apiFetch("/api/v1/live/trades", {}, token);

export default function DashboardPage(): React.ReactElement {
  const equityCurve = useMemo(() => generateEquityCurve(), []);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Overview of your trading performance and market signals
        </p>
      </div>

      {/* Stats grid */}
      <PortfolioSummary />

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
