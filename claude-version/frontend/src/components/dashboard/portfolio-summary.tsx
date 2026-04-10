"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  TrendingUp,
  TrendingDown,
  DollarSign,
  Activity,
  Percent,
} from "lucide-react";
import { activeStrategies } from "@/lib/mock-data/dashboard";

export interface PortfolioSummaryProps {
  /** Override the active-strategies count (e.g. from the API). */
  totalStrategies?: number;
  runningStrategies?: number;
  /** Real account data from IB Gateway (null = use hardcoded fallback). */
  accountData?: {
    net_liquidation: number;
    unrealized_pnl: number;
  } | null;
}

interface StatCardProps {
  title: string;
  value: string;
  change: string;
  trend: "up" | "down";
  icon: React.ComponentType<{ className?: string }>;
}

function StatCard({
  title,
  value,
  change,
  trend,
  icon: Icon,
}: StatCardProps): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <Icon className="size-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tracking-tight">{value}</div>
        <p
          className={`mt-1 flex items-center gap-1 text-xs ${
            trend === "up" ? "text-emerald-500" : "text-red-500"
          }`}
        >
          {trend === "up" ? (
            <TrendingUp className="size-3" />
          ) : (
            <TrendingDown className="size-3" />
          )}
          {change}
        </p>
      </CardContent>
    </Card>
  );
}

export function PortfolioSummary({
  totalStrategies,
  runningStrategies,
  accountData,
}: PortfolioSummaryProps = {}): React.ReactElement {
  const fallbackRunning = activeStrategies.filter(
    (s) => s.status === "running",
  ).length;
  const total = totalStrategies ?? activeStrategies.length;
  const running = runningStrategies ?? fallbackRunning;

  // Use real account data if available, otherwise hardcoded fallback
  const totalValue = accountData?.net_liquidation ?? 125_430.56;
  const dailyPnl = accountData?.unrealized_pnl ?? 1_234.56;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        title="Total Value"
        value={`$${totalValue.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
        change={accountData ? "From IB Gateway" : "Mock data"}
        trend="up"
        icon={DollarSign}
      />
      <StatCard
        title="Daily P&L"
        value={`${dailyPnl >= 0 ? "+" : ""}$${Math.abs(dailyPnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
        change={accountData ? "From IB Gateway" : "Mock data"}
        trend={dailyPnl >= 0 ? "up" : "down"}
        icon={TrendingUp}
      />
      <StatCard
        title="Total Return"
        value="+12.5%"
        change="Since inception"
        trend="up"
        icon={Percent}
      />
      <StatCard
        title="Active Strategies"
        value={`${running}/${total}`}
        change={`${running} running`}
        trend="up"
        icon={Activity}
      />
    </div>
  );
}
