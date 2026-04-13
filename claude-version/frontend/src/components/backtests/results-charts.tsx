"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  BarChart3,
  TrendingUp,
  TrendingDown,
  Trophy,
  Activity,
  Zap,
} from "lucide-react";
import type { EquityPoint } from "@/lib/api";
import { formatCurrency, formatPercent } from "@/lib/format";

export interface ResultsChartsBacktest {
  sharpeRatio: number;
  sortinoRatio: number;
  maxDrawdown: number; // percent (e.g. -8.3 for -8.3%)
  totalReturn: number; // percent (e.g. 24.5 for 24.5%)
  winRate: number; // percent (e.g. 62.3 for 62.3%)
  totalTrades: number;
}

function MetricCard({
  title,
  value,
  icon: Icon,
  color,
}: {
  title: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
  color?: string;
}): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <Icon className="size-4 text-muted-foreground" />
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-semibold ${color ?? ""}`}>{value}</div>
      </CardContent>
    </Card>
  );
}

function MonthlyReturnsHeatmap(): React.ReactElement {
  return (
    <div className="flex h-24 items-center justify-center text-sm text-muted-foreground">
      Monthly returns data not yet available from the backend.
    </div>
  );
}

interface ResultsChartsProps {
  backtest: ResultsChartsBacktest;
  equityCurve: EquityPoint[];
}

export function ResultsCharts({
  backtest,
  equityCurve,
}: ResultsChartsProps): React.ReactElement {
  return (
    <>
      {/* Key metrics grid */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
        <MetricCard
          title="Sharpe Ratio"
          value={backtest.sharpeRatio.toFixed(2)}
          icon={BarChart3}
        />
        <MetricCard
          title="Sortino Ratio"
          value={backtest.sortinoRatio.toFixed(2)}
          icon={Activity}
        />
        <MetricCard
          title="Max Drawdown"
          value={formatPercent(backtest.maxDrawdown)}
          icon={TrendingDown}
          color="text-red-500"
        />
        <MetricCard
          title="Total Return"
          value={formatPercent(backtest.totalReturn)}
          icon={TrendingUp}
          color={
            backtest.totalReturn >= 0 ? "text-emerald-500" : "text-red-500"
          }
        />
        <MetricCard
          title="Win Rate"
          value={`${backtest.winRate.toFixed(1)}%`}
          icon={Trophy}
        />
        <MetricCard
          title="Total Trades"
          value={backtest.totalTrades.toString()}
          icon={Zap}
        />
      </div>

      {/* Equity curve */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Equity Curve</CardTitle>
          <CardDescription>
            Portfolio value over the backtest period
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={equityCurve}
                margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(0 0% 50% / 0.1)"
                />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: string) => {
                    const d = new Date(v);
                    return `${d.getMonth() + 1}/${d.getDate()}`;
                  }}
                  interval={30}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                  domain={["dataMin - 2000", "dataMax + 2000"]}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "hsl(0 0% 12.7%)",
                    border: "1px solid hsl(0 0% 100% / 0.1)",
                    borderRadius: "8px",
                    fontSize: "12px",
                  }}
                  labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                  formatter={(value: number | undefined) => [
                    formatCurrency(value ?? 0),
                    "Equity",
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey="equity"
                  stroke="hsl(142, 76%, 36%)"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Drawdown chart */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Drawdown</CardTitle>
          <CardDescription>Portfolio drawdown from peak equity</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={equityCurve}
                margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
              >
                <defs>
                  <linearGradient
                    id="drawdownGradient"
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop
                      offset="0%"
                      stopColor="hsl(0, 84%, 60%)"
                      stopOpacity={0.4}
                    />
                    <stop
                      offset="100%"
                      stopColor="hsl(0, 84%, 60%)"
                      stopOpacity={0}
                    />
                  </linearGradient>
                </defs>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(0 0% 50% / 0.1)"
                />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: string) => {
                    const d = new Date(v);
                    return `${d.getMonth() + 1}/${d.getDate()}`;
                  }}
                  interval={30}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                  domain={["dataMin - 1", 0]}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "hsl(0 0% 12.7%)",
                    border: "1px solid hsl(0 0% 100% / 0.1)",
                    borderRadius: "8px",
                    fontSize: "12px",
                  }}
                  labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                  formatter={(value: number | undefined) => [
                    `${(value ?? 0).toFixed(2)}%`,
                    "Drawdown",
                  ]}
                />
                <Area
                  type="monotone"
                  dataKey="drawdown"
                  stroke="hsl(0, 84%, 60%)"
                  strokeWidth={1.5}
                  fill="url(#drawdownGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Monthly returns heatmap */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Monthly Returns</CardTitle>
          <CardDescription>Return breakdown by month and year</CardDescription>
        </CardHeader>
        <CardContent>
          <MonthlyReturnsHeatmap />
        </CardContent>
      </Card>
    </>
  );
}
