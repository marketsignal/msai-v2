"use client";

import { useQuery } from "@tanstack/react-query";
import { Card, CardContent } from "@/components/ui/card";
import { AlertTriangle } from "lucide-react";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { AlertsFeed } from "@/components/dashboard/alerts-feed";
import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { RecentTrades } from "@/components/dashboard/recent-trades";
import {
  apiGet,
  describeApiError,
  getAccountSummary,
  getLiveStatus,
  type AccountSummary,
  type LiveStatusResponse,
  type StrategyListResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Dashboard — landing page after sign-in.
 *
 * Migrated to three independent TanStack Query hooks per silent-failure
 * hunter F5: each subsystem renders its own error state instead of all
 * three failures collapsing to a single boolean flag and producing
 * misleading $0.00 / 0-running displays during transient outages.
 */
export default function DashboardPage(): React.ReactElement {
  const { getToken, isAuthenticated } = useAuth();

  const strategiesQuery = useQuery<StrategyListResponse, Error>({
    queryKey: ["dashboard", "strategies-count"],
    queryFn: async (): Promise<StrategyListResponse> => {
      const token = await getToken();
      return apiGet<StrategyListResponse>("/api/v1/strategies/", token);
    },
    enabled: isAuthenticated,
    staleTime: 30_000,
  });

  const accountQuery = useQuery<AccountSummary, Error>({
    queryKey: ["dashboard", "account-summary"],
    queryFn: async (): Promise<AccountSummary> => {
      const token = await getToken();
      return getAccountSummary(token);
    },
    enabled: isAuthenticated,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const liveQuery = useQuery<LiveStatusResponse, Error>({
    queryKey: ["dashboard", "live-status"],
    queryFn: async (): Promise<LiveStatusResponse> => {
      const token = await getToken();
      return getLiveStatus(token);
    },
    enabled: isAuthenticated,
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  const deployments = liveQuery.data?.deployments ?? [];
  const runningCount = deployments.filter((d) => d.status === "running").length;

  const errors: { label: string; message: string }[] = [];
  if (strategiesQuery.isError) {
    errors.push({
      label: "Strategies",
      message: describeApiError(
        strategiesQuery.error,
        "Strategies fetch failed",
      ),
    });
  }
  if (accountQuery.isError) {
    errors.push({
      label: "Account",
      message: describeApiError(accountQuery.error, "Account fetch failed"),
    });
  }
  if (liveQuery.isError) {
    errors.push({
      label: "Live status",
      message: describeApiError(liveQuery.error, "Live status fetch failed"),
    });
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Overview of your trading performance and market signals.
        </p>
      </div>

      {errors.length > 0 && (
        <Card className="border-red-500/30" role="alert">
          <CardContent className="space-y-2 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-red-400">
              <AlertTriangle className="size-4" aria-hidden="true" />
              {errors.length === 1
                ? "One data source failed to load"
                : `${errors.length} data sources failed to load`}
            </div>
            <ul className="space-y-1 text-xs text-muted-foreground">
              {errors.map((e) => (
                <li key={e.label}>
                  <span className="font-medium">{e.label}:</span>{" "}
                  <span className="font-mono">{e.message}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Stats grid (drops "Total Value: $0.00" when account is unavailable —
          PortfolioSummary now renders "—" + neutral trend in that case) */}
      <PortfolioSummary
        totalStrategies={strategiesQuery.data?.total}
        runningStrategies={runningCount}
        accountData={accountQuery.data ?? null}
        totalUnavailable={strategiesQuery.isError}
        runningUnavailable={liveQuery.isError}
        accountUnavailable={accountQuery.isError}
      />

      {/* Recent alerts + Active strategies (was permanently-empty EquityChart) */}
      <div className="grid gap-6 lg:grid-cols-7">
        <AlertsFeed limit={5} />
        <ActiveStrategies
          deployments={deployments}
          unavailable={liveQuery.isError}
        />
      </div>

      {/* Recent trades */}
      <RecentTrades />
    </div>
  );
}
