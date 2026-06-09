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
import { useAccountScope } from "@/lib/account-scope";
import { useMemo } from "react";

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
  const { scope } = useAccountScope();

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
      // activeOnly: scoped running-count + ActiveStrategies must reflect the
      // COMPLETE active set, not the default 50-most-recent cap that could drop
      // a long-running active deployment (Codex code-review iter-7 P2).
      return getLiveStatus(token, { activeOnly: true });
    },
    enabled: isAuthenticated,
    refetchInterval: 15_000,
    staleTime: 10_000,
  });

  // US-005: the connected gateway account id labels the (gateway-bound) balance
  // cards. Codex code-review iter-5 P2: source it from the SUMMARY payload
  // (`accountQuery.data.account_id`) — NOT a separate /account/health query —
  // so the label and the balances it names always come from the SAME fetch and
  // can never diverge (health refreshing to account B while the cached summary
  // still holds account A's numbers). Stays UNSCOPED: the balances are the
  // connected account's, never the selector's scope.

  const deployments = useMemo(
    () => liveQuery.data?.deployments ?? [],
    [liveQuery.data],
  );
  // US-001: scope the deployment-derived cards by the global account selector.
  const scopedDeployments = useMemo(() => {
    if (scope === "all") return deployments;
    if (scope === "unassigned") return deployments.filter((d) => !d.account_id);
    return deployments.filter((d) => d.account_id === scope);
  }, [deployments, scope]);
  const runningCount = scopedDeployments.filter(
    (d) => d.status === "running",
  ).length;

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
        connectedAccountId={accountQuery.data?.account_id ?? null}
      />

      {/* Recent alerts + Active strategies (was permanently-empty EquityChart) */}
      <div className="grid gap-6 lg:grid-cols-7">
        <AlertsFeed limit={5} />
        <ActiveStrategies
          deployments={scopedDeployments}
          unavailable={liveQuery.isError}
        />
      </div>

      {/* Recent trades */}
      <RecentTrades />
    </div>
  );
}
