"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { RotateCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { AccountSummaryCard } from "@/components/account/account-summary-card";
import { AccountPortfolioTable } from "@/components/account/account-portfolio-table";
import { AccountHealthCard } from "@/components/account/account-health-card";
import {
  useAccountSummary,
  useAccountPortfolio,
  useAccountHealth,
  ACCOUNT_SUMMARY_KEY,
  ACCOUNT_PORTFOLIO_KEY,
  ACCOUNT_HEALTH_KEY,
} from "@/lib/hooks/use-account";

export default function AccountPage(): React.ReactElement {
  const summary = useAccountSummary();
  const portfolio = useAccountPortfolio();
  const health = useAccountHealth();
  const qc = useQueryClient();

  const refreshAll = (): void => {
    void qc.invalidateQueries({ queryKey: ACCOUNT_SUMMARY_KEY });
    void qc.invalidateQueries({ queryKey: ACCOUNT_PORTFOLIO_KEY });
    void qc.invalidateQueries({ queryKey: ACCOUNT_HEALTH_KEY });
  };

  const latestRefresh = Math.max(
    summary.dataUpdatedAt,
    portfolio.dataUpdatedAt,
    health.dataUpdatedAt,
  );

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Broker account
          </h1>
          <p className="text-sm text-muted-foreground">
            IBKR account data served from the long-lived snapshot — refreshes
            every 30 s in the background.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {latestRefresh > 0 && (
            <p className="font-mono text-xs text-muted-foreground">
              Last refresh{" "}
              {new Date(latestRefresh).toLocaleTimeString("en-US", {
                hour12: false,
              })}
            </p>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={refreshAll}
            className="gap-2"
          >
            <RotateCw className="size-3.5" aria-hidden="true" />
            Refresh
          </Button>
        </div>
      </div>

      <Tabs defaultValue="summary" className="space-y-4">
        <TabsList>
          <TabsTrigger value="summary" data-testid="tab-summary">
            Summary
          </TabsTrigger>
          <TabsTrigger value="portfolio" data-testid="tab-portfolio">
            Portfolio
          </TabsTrigger>
          <TabsTrigger value="health" data-testid="tab-health">
            Health
          </TabsTrigger>
        </TabsList>

        <TabsContent value="summary">
          <AccountSummaryCard
            data={summary.data}
            isPending={summary.isPending}
            error={summary.error}
          />
        </TabsContent>

        <TabsContent value="portfolio">
          <AccountPortfolioTable
            data={portfolio.data}
            isPending={portfolio.isPending}
            error={portfolio.error}
          />
        </TabsContent>

        <TabsContent value="health">
          <AccountHealthCard
            data={health.data}
            isPending={health.isPending}
            error={health.error}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
