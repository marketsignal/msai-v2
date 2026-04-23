"use client";

import { useMemo } from "react";
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
import { SeriesStatusIndicator } from "@/components/backtests/series-status-indicator";
import type {
  SeriesMonthlyReturn,
  SeriesPayload,
  SeriesStatus,
} from "@/lib/api";
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

// ---------------------------------------------------------------------------
// Monthly returns heatmap — native CSS Grid (no Recharts heatmap primitive)
// ---------------------------------------------------------------------------

const MONTH_LABELS = [
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

// Heatmap cell color — green for gains, red for losses, dark gray for empty.
// Intensity scales with |pct| so larger moves are visually bolder.
const HEATMAP_HUE_GAIN = 145;
const HEATMAP_HUE_LOSS = 25;
const HEATMAP_EMPTY_FILL = "oklch(0.18 0 0)";
const HEATMAP_CHROMA_CAP = 0.25;
const HEATMAP_CHROMA_SLOPE = 2;
const HEATMAP_LIGHTNESS_BASE = 0.45;
const HEATMAP_LIGHTNESS_CAP = 0.15;
const HEATMAP_LIGHTNESS_SLOPE = 1.5;

function cellColor(pct: number | undefined): string {
  if (pct === undefined) return HEATMAP_EMPTY_FILL;
  const hue = pct >= 0 ? HEATMAP_HUE_GAIN : HEATMAP_HUE_LOSS;
  const magnitude = Math.abs(pct);
  const chroma = Math.min(HEATMAP_CHROMA_CAP, magnitude * HEATMAP_CHROMA_SLOPE);
  const lightness =
    HEATMAP_LIGHTNESS_BASE +
    Math.min(HEATMAP_LIGHTNESS_CAP, magnitude * HEATMAP_LIGHTNESS_SLOPE);
  return `oklch(${lightness} ${chroma} ${hue})`;
}

interface MonthlyReturnsHeatmapProps {
  monthly: SeriesMonthlyReturn[];
}

function MonthlyReturnsHeatmap({
  monthly,
}: MonthlyReturnsHeatmapProps): React.ReactElement {
  // Pivot to {year → {month → pct}} so the grid can render dense rows with
  // blanks where the backtest window didn't span a particular month.
  // Memoized — the pivot is O(n) over ``monthly`` but gets rebuilt whenever
  // the parent re-renders (e.g. tab switch, poll tick). Memoizing on the
  // array reference keeps the heatmap stable across unrelated re-renders.
  // NOTE: ``useMemo`` must run on every render (React rules-of-hooks), so
  // the empty-state early return comes AFTER the hook call.
  const { byYear, years } = useMemo(() => {
    const map = new Map<string, Map<string, number>>();
    for (const { month, pct } of monthly) {
      const [yr, mo] = month.split("-");
      if (!map.has(yr)) map.set(yr, new Map());
      map.get(yr)?.set(mo, pct);
    }
    return { byYear: map, years: Array.from(map.keys()).sort() };
  }, [monthly]);

  if (monthly.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No monthly data available.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto" data-testid="monthly-returns-heatmap">
      <div
        className="grid gap-1 text-xs"
        style={{
          gridTemplateColumns: `auto repeat(12, minmax(2.5rem, 1fr))`,
        }}
      >
        <div />
        {MONTH_LABELS.map((m) => (
          <div key={m} className="text-center text-muted-foreground">
            {m}
          </div>
        ))}
        {years.map((yr) => (
          <YearRow key={yr} year={yr} months={byYear.get(yr)} />
        ))}
      </div>
    </div>
  );
}

function YearRow({
  year,
  months,
}: {
  year: string;
  months: Map<string, number> | undefined;
}): React.ReactElement {
  return (
    <>
      <div className="flex items-center pr-2 text-muted-foreground">{year}</div>
      {MONTH_LABELS.map((_, idx) => {
        const moKey = String(idx + 1).padStart(2, "0");
        const pct = months?.get(moKey);
        return (
          <div
            key={moKey}
            className="flex h-8 items-center justify-center rounded text-[10px] font-medium text-foreground"
            style={{ backgroundColor: cellColor(pct) }}
            title={
              pct !== undefined
                ? `${year}-${moKey}: ${(pct * 100).toFixed(2)}%`
                : "No data"
            }
          >
            {pct !== undefined ? `${(pct * 100).toFixed(1)}` : ""}
          </div>
        );
      })}
    </>
  );
}

/**
 * Empty-state panel for ``series_status === "ready"`` + zero daily rows.
 *
 * Distinct from ``<SeriesStatusIndicator>`` which renders legacy / failed
 * cases. A legitimate zero-trade backtest lands here with a clear message
 * rather than a silently blank chart card — otherwise the user can't tell
 * an empty run from a broken render.
 */
function EmptySeriesPanel(): React.ReactElement {
  return (
    <div
      className="flex flex-col items-center justify-center gap-2 py-10 text-muted-foreground"
      data-testid="series-empty"
    >
      <p className="text-sm">No chart data for this backtest.</p>
      <p className="text-xs">
        The run completed without generating enough returns to plot — check the
        Trade Log below.
      </p>
    </div>
  );
}

/**
 * Format ``YYYY-MM-DD`` as ``M/D`` without timezone drift.
 *
 * ``new Date("2024-01-02")`` parses as UTC midnight, so local
 * ``getMonth()``/``getDate()`` in a negative-offset timezone returns the
 * previous calendar day. Parse components directly — the series is a
 * calendar date, not a wall-clock instant.
 */
function formatTickDate(isoDate: string): string {
  const parts = isoDate.split("-");
  if (parts.length !== 3) return isoDate;
  const month = Number.parseInt(parts[1], 10);
  const day = Number.parseInt(parts[2], 10);
  if (Number.isNaN(month) || Number.isNaN(day)) return isoDate;
  return `${month}/${day}`;
}

/** Show roughly one X-axis tick per month on the daily series. */
const DAILY_CHART_TICK_INTERVAL = 30;

// ---------------------------------------------------------------------------
// Main component — wires equity + drawdown to series.daily
// ---------------------------------------------------------------------------

interface ResultsChartsProps {
  backtest: ResultsChartsBacktest;
  series: SeriesPayload | null;
  seriesStatus: SeriesStatus;
}

export function ResultsCharts({
  backtest,
  series,
  seriesStatus,
}: ResultsChartsProps): React.ReactElement {
  const daily = series?.daily ?? [];
  const monthly = series?.monthly_returns ?? [];
  const hasSeries = seriesStatus === "ready" && daily.length > 0;

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
          {hasSeries ? (
            <div className="h-72" data-testid="equity-curve-chart">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={daily}
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
                    tickFormatter={formatTickDate}
                    interval={DAILY_CHART_TICK_INTERVAL}
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
          ) : seriesStatus === "ready" ? (
            <EmptySeriesPanel />
          ) : (
            <SeriesStatusIndicator status={seriesStatus} />
          )}
        </CardContent>
      </Card>

      {/* Drawdown */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Drawdown</CardTitle>
          <CardDescription>Portfolio drawdown from peak equity</CardDescription>
        </CardHeader>
        <CardContent>
          {hasSeries ? (
            <div className="h-48" data-testid="drawdown-chart">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={daily}
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
                    tickFormatter={formatTickDate}
                    interval={DAILY_CHART_TICK_INTERVAL}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                    tickLine={false}
                    axisLine={false}
                    // drawdown is a ratio (e.g. -0.083); render as percent.
                    tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`}
                    domain={["dataMin", 0]}
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
                      `${((value ?? 0) * 100).toFixed(2)}%`,
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
          ) : seriesStatus === "ready" ? (
            <EmptySeriesPanel />
          ) : (
            <SeriesStatusIndicator status={seriesStatus} />
          )}
        </CardContent>
      </Card>

      {/* Monthly returns heatmap */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Monthly Returns</CardTitle>
          <CardDescription>Return breakdown by month and year</CardDescription>
        </CardHeader>
        <CardContent>
          {seriesStatus === "ready" ? (
            // Heatmap owns its own empty-state copy when monthly[] is empty.
            <MonthlyReturnsHeatmap monthly={monthly} />
          ) : (
            <SeriesStatusIndicator status={seriesStatus} />
          )}
        </CardContent>
      </Card>
    </>
  );
}
