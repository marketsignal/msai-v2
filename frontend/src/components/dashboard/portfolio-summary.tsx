"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  DollarSign,
  Activity,
} from "lucide-react";

export interface PortfolioSummaryProps {
  /** Override the active-strategies count (e.g. from the API). */
  totalStrategies?: number;
  runningStrategies?: number;
  /** Real account data from IB Gateway (null = not yet loaded). */
  accountData?: {
    net_liquidation: number;
    unrealized_pnl: number;
  } | null;
  /**
   * Codex iter-3 P2-A: signal that the source data for the Active
   * Strategies card is unavailable (parent's query is in ``isError``).
   * Without this the card defaulted to ``0/0`` + "No strategies
   * registered" — indistinguishable from a real empty registry. With
   * the flag set, the card renders "—" + "Unavailable" so the user
   * cannot read 0 as truth.
   */
  totalUnavailable?: boolean;
  runningUnavailable?: boolean;
  /**
   * Mirror of the strategies-unavailable flag for the IB account cards
   * (Total Value, Daily P&L). When the account query errors we cannot
   * tell "no account connected" from "couldn't reach backend"; the flag
   * forces the subtitle to "Unavailable" so the user doesn't read "No
   * account connected" as a confirmed fact.
   */
  accountUnavailable?: boolean;
}

interface StatCardProps {
  title: string;
  value: string;
  change: string;
  trend: "up" | "down" | "neutral";
  icon: React.ComponentType<{ className?: string }>;
}

const TREND_COLOR: Record<StatCardProps["trend"], string> = {
  up: "text-emerald-500",
  down: "text-red-500",
  neutral: "text-muted-foreground",
};

const TREND_ICON: Record<StatCardProps["trend"], typeof TrendingUp> = {
  up: TrendingUp,
  down: TrendingDown,
  neutral: Minus,
};

/** Map a signed numeric value to a trend bucket, with ``hasData`` gating
 *  zero/unknown sources to ``neutral``. */
function signTrend(value: number, hasData: boolean): StatCardProps["trend"] {
  if (!hasData) return "neutral";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "neutral";
}

function StatCard({
  title,
  value,
  change,
  trend,
  icon: Icon,
}: StatCardProps): React.ReactElement {
  const trendColor = TREND_COLOR[trend];
  const TrendIcon = TREND_ICON[trend];
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
        <p className={`mt-1 flex items-center gap-1 text-xs ${trendColor}`}>
          <TrendIcon className="size-3" aria-hidden="true" />
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
  totalUnavailable,
  runningUnavailable,
  accountUnavailable,
}: PortfolioSummaryProps = {}): React.ReactElement {
  const total = totalStrategies ?? 0;
  const running = runningStrategies ?? 0;
  // If either source errored we cannot trust the displayed count — render
  // "—" / "Unavailable" so the operator doesn't read 0 as a fact. Either
  // flag set degrades the card.
  const strategiesUnavailable = Boolean(totalUnavailable || runningUnavailable);

  // Use real account data if available, otherwise show zero.
  // Per audit F-9/F-10: drop the permanently "--" Total Return card,
  // and drive trend arrows from the actual value sign — no hardcoded "up".
  const totalValue = accountData?.net_liquidation ?? 0;
  const dailyPnl = accountData?.unrealized_pnl ?? 0;
  const hasAccount = accountData != null;

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <StatCard
        title="Total Value"
        value={
          hasAccount
            ? `$${totalValue.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
            : "—"
        }
        change={
          accountUnavailable
            ? "Unavailable"
            : hasAccount
              ? "From IB Gateway"
              : "No account connected"
        }
        trend={signTrend(totalValue, hasAccount)}
        icon={DollarSign}
      />
      <StatCard
        title="Daily P&L"
        value={
          hasAccount
            ? `${dailyPnl > 0 ? "+" : dailyPnl < 0 ? "-" : ""}$${Math.abs(dailyPnl).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
            : "—"
        }
        change={
          accountUnavailable
            ? "Unavailable"
            : hasAccount
              ? "From IB Gateway"
              : "No account connected"
        }
        trend={signTrend(dailyPnl, hasAccount)}
        icon={TrendingUp}
      />
      <StatCard
        title="Active Strategies"
        value={strategiesUnavailable ? "—" : `${running}/${total}`}
        change={
          strategiesUnavailable
            ? "Unavailable"
            : total === 0
              ? "No strategies registered"
              : running > 0
                ? `${running} running`
                : "All idle"
        }
        trend={
          strategiesUnavailable ? "neutral" : running > 0 ? "up" : "neutral"
        }
        icon={Activity}
      />
    </div>
  );
}
