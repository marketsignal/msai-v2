"use client";

import { useMemo } from "react";
import Link from "next/link";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  TrendingUp,
  TrendingDown,
  DollarSign,
  Activity,
  Percent,
  Zap,
} from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import {
  generateEquityCurve,
  recentTrades,
  activeStrategies,
} from "@/lib/mock-data/dashboard";
import {
  formatCurrency,
  formatSignedCurrency,
  formatDateTime,
} from "@/lib/format";

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

function statusColor(status: "running" | "stopped" | "error"): string {
  switch (status) {
    case "running":
      return "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "bg-muted text-muted-foreground hover:bg-muted";
    case "error":
      return "bg-red-500/15 text-red-500 hover:bg-red-500/25";
  }
}

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

  const runningCount = activeStrategies.filter(
    (s) => s.status === "running",
  ).length;

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
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="Total Value"
          value="$125,430.56"
          change="+$2,340.12 from yesterday"
          trend="up"
          icon={DollarSign}
        />
        <StatCard
          title="Daily P&L"
          value="+$1,234.56"
          change="+0.99% today"
          trend="up"
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
          value={`${runningCount}/${activeStrategies.length}`}
          change={`${runningCount} running`}
          trend="up"
          icon={Activity}
        />
      </div>

      {/* Equity curve + Active strategies */}
      <div className="grid gap-6 lg:grid-cols-7">
        <Card className="border-border/50 lg:col-span-4">
          <CardHeader>
            <CardTitle className="text-base">Portfolio Performance</CardTitle>
            <CardDescription>
              Equity curve over the last 30 days
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={equityCurve}
                  margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                >
                  <defs>
                    <linearGradient
                      id="equityGradient"
                      x1="0"
                      y1="0"
                      x2="0"
                      y2="1"
                    >
                      <stop
                        offset="0%"
                        stopColor="hsl(142, 76%, 36%)"
                        stopOpacity={0.3}
                      />
                      <stop
                        offset="100%"
                        stopColor="hsl(142, 76%, 36%)"
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
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                    tickLine={false}
                    axisLine={false}
                    tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                    domain={["dataMin - 1000", "dataMax + 1000"]}
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
                      "Portfolio",
                    ]}
                  />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke="hsl(142, 76%, 36%)"
                    strokeWidth={2}
                    fill="url(#equityGradient)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border/50 lg:col-span-3">
          <CardHeader>
            <CardTitle className="text-base">Active Strategies</CardTitle>
            <CardDescription>Status of all deployed strategies</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {activeStrategies.map((strategy) => (
                <Link
                  key={strategy.id}
                  href={`/strategies/${strategy.id}`}
                  className="flex items-center justify-between rounded-lg border border-border/50 p-3 transition-colors hover:bg-accent/50"
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium">{strategy.name}</p>
                      <Badge
                        variant="secondary"
                        className={statusColor(strategy.status)}
                      >
                        {strategy.status}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {strategy.instruments.join(", ")}
                    </p>
                  </div>
                  {strategy.status === "running" && (
                    <span
                      className={`text-sm font-medium ${
                        strategy.dailyPnl >= 0
                          ? "text-emerald-500"
                          : "text-red-500"
                      }`}
                    >
                      {formatSignedCurrency(strategy.dailyPnl)}
                    </span>
                  )}
                </Link>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Recent trades */}
      <Card className="border-border/50">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Zap className="size-4 text-muted-foreground" />
            <CardTitle className="text-base">Recent Trades</CardTitle>
          </div>
          <CardDescription>Last 10 executed trades</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Price</TableHead>
                <TableHead className="text-right">P&L</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {recentTrades.map((trade) => (
                <TableRow key={trade.id} className="border-border/50">
                  <TableCell className="text-muted-foreground">
                    {formatDateTime(trade.timestamp)}
                  </TableCell>
                  <TableCell className="font-medium">
                    {trade.instrument}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={
                        trade.side === "BUY"
                          ? "bg-emerald-500/15 text-emerald-500"
                          : "bg-red-500/15 text-red-500"
                      }
                    >
                      {trade.side}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">{trade.quantity}</TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(trade.price)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-medium ${
                      trade.pnl >= 0 ? "text-emerald-500" : "text-red-500"
                    }`}
                  >
                    {formatSignedCurrency(trade.pnl)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
