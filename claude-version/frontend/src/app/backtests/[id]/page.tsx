"use client";

import { use, useMemo } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ArrowLeft,
  Download,
  BarChart3,
  TrendingUp,
  TrendingDown,
  Trophy,
  Activity,
  Zap,
} from "lucide-react";
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
  getBacktestById,
  generateEquityCurve,
  generateMonthlyReturns,
  backtestTrades,
} from "@/lib/mock-data/backtests";
import {
  formatCurrency,
  formatSignedCurrency,
  formatPercent,
  formatDateTime,
} from "@/lib/format";

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
  const data = useMemo(() => generateMonthlyReturns(), []);
  const months = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];
  const years = [2024, 2025];

  function getCellColor(val: number): string {
    if (val >= 5) return "bg-emerald-600 text-white";
    if (val >= 2) return "bg-emerald-500/40 text-emerald-300";
    if (val >= 0) return "bg-emerald-500/15 text-emerald-400";
    if (val >= -2) return "bg-red-500/15 text-red-400";
    if (val >= -5) return "bg-red-500/40 text-red-300";
    return "bg-red-600 text-white";
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr>
            <th className="p-2 text-left font-medium text-muted-foreground">
              Year
            </th>
            {months.map((m) => (
              <th
                key={m}
                className="p-2 text-center font-medium text-muted-foreground"
              >
                {m}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {years.map((year) => (
            <tr key={year}>
              <td className="p-2 font-medium">{year}</td>
              {months.map((month) => {
                const entry = data.find(
                  (d) => d.month === month && d.year === year,
                );
                const val = entry?.return_pct ?? 0;
                return (
                  <td key={month} className="p-1">
                    <div
                      className={`rounded-md px-2 py-1.5 text-center font-mono ${getCellColor(val)}`}
                    >
                      {val >= 0 ? "+" : ""}
                      {val.toFixed(1)}%
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function BacktestDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): React.ReactElement {
  const { id } = use(params);
  const router = useRouter();
  const backtest = getBacktestById(id);
  const equityCurve = useMemo(
    () => (backtest ? generateEquityCurve(backtest.totalReturn) : []),
    [backtest],
  );

  if (!backtest) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-4">
        <p className="text-muted-foreground">Backtest not found</p>
        <Button asChild variant="outline">
          <Link href="/backtests">Back to Backtests</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Back + header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => router.push("/backtests")}
          >
            <ArrowLeft className="size-4" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                {backtest.strategyName} Backtest
              </h1>
              <Badge
                variant="secondary"
                className="bg-emerald-500/15 text-emerald-500"
              >
                {backtest.status}
              </Badge>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {backtest.dateRange} &middot; {backtest.instruments.join(", ")}
            </p>
          </div>
        </div>
        <Button variant="outline" className="gap-1.5" asChild>
          <a href={`/api/v1/backtests/${backtest.id}/report`}>
            <Download className="size-3.5" />
            Download Report
          </a>
        </Button>
      </div>

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

      {/* Trade log */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Trade Log</CardTitle>
          <CardDescription>
            Individual trades executed during the backtest
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Timestamp</TableHead>
                <TableHead>Instrument</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">Exit</TableHead>
                <TableHead className="text-right">P&L</TableHead>
                <TableHead className="text-right">Duration</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backtestTrades.map((trade) => (
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
                    {formatCurrency(trade.entryPrice)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCurrency(trade.exitPrice)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-medium ${
                      trade.pnl >= 0 ? "text-emerald-500" : "text-red-500"
                    }`}
                  >
                    {formatSignedCurrency(trade.pnl)}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    {trade.holdingPeriod}
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
