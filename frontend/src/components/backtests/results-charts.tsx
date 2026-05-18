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
import { formatPercent } from "@/lib/format";

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

/**
 * Compute an adaptive Y-axis domain for the equity curve.
 *
 * Why: hardcoded ``[dataMin - 2000, dataMax + 2000]`` made small-return
 * backtests render as a flat line because the data range (e.g. $0.74 over
 * a 0.08% return on $100k) was dwarfed by the ±$2,000 padding. Use the
 * larger of (5% of range) and (0.05% of dataMin, i.e. ~$50 on a $100k
 * account) so micro-returns still show curvature without huge-equity
 * backtests being too tight.
 *
 * Returns a Recharts-compatible ``[min, max]`` tuple. Empty data → [0, 0]
 * (Recharts handles gracefully — the chart renders no line in that case).
 */
/**
 * Format a cumulative-return-% value for the equity-curve Y-axis.
 *
 * Pablo 2026-05-17: prefers cumulative return % over raw dollar equity
 * because % growth compounds visibly over the backtest window — small
 * early returns ARE visible against the rest of the trajectory rather
 * than being dwarfed by the $-magnitude.
 *
 * Adaptive precision: when the cumulative-return range is large
 * (multi-percent), 1 decimal is enough. When it's a micro-return run
 * (<0.5% total), bump to 2–3 decimals.
 */
function returnTickFormatter(cumReturnPct: number[]): (v: number) => string {
  if (cumReturnPct.length === 0) return (v) => `${v.toFixed(2)}%`;
  let min = cumReturnPct[0];
  let max = cumReturnPct[0];
  for (const v of cumReturnPct) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const range = max - min;
  // step ≈ range / 8 ticks. Pick decimals so the step is at least one
  // unit on the displayed scale, capped at 3 decimals for readability.
  const step = range / 8;
  let decimals = 2;
  if (step > 0 && step < 0.1) {
    decimals = Math.min(3, Math.max(2, Math.ceil(-Math.log10(step))));
  } else if (step >= 1) {
    decimals = 1;
  }
  return (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(decimals)}%`;
}

/**
 * Compute an adaptive Y-axis domain for the cumulative-return % series.
 * Same pattern as ``equityYDomain`` but operates on percent values: pads
 * by max(5% of range, 0.001%, ~0.01% of base scale) so a flat / micro
 * return series still shows curvature with breathing room above/below.
 */
/**
 * Drawdown tick formatter with adaptive precision. The backend stores
 * drawdown as a ratio (-0.083 = -8.3%); pre-fix every label collapsed
 * to "-0.0%" for micro-drawdown runs. Picks decimals from the data
 * magnitude so small drawdowns still differentiate ticks.
 */
function drawdownTickFormatter(
  daily: { drawdown: number }[],
): (v: number) => string {
  if (daily.length === 0) return (v) => `${(v * 100).toFixed(1)}%`;
  let mostNegative = 0;
  for (const p of daily) {
    if (p.drawdown < mostNegative) mostNegative = p.drawdown;
  }
  // mostNegative is in ratio form; convert to displayed % magnitude.
  const magnitudePct = Math.abs(mostNegative) * 100;
  let decimals = 1;
  if (magnitudePct > 0 && magnitudePct < 1) {
    decimals = Math.min(4, Math.max(2, Math.ceil(-Math.log10(magnitudePct))));
  }
  return (v: number) => `${(v * 100).toFixed(decimals)}%`;
}

function returnYDomain(cumReturnPct: number[]): [number, number] {
  if (cumReturnPct.length === 0) return [0, 0];
  let min = cumReturnPct[0];
  let max = cumReturnPct[0];
  for (const v of cumReturnPct) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const range = max - min;
  const padding = Math.max(range * 0.05, 0.001);
  return [min - padding, max + padding];
}

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

  // Pablo 2026-05-17: equity curve is more useful as cumulative-return
  // % over time. The raw $ equity makes small returns invisible (a 0.08%
  // gain on $100k is $80 of variation in a ~$100k Y-axis); plotting
  // ``(equity_t / equity_0 - 1) * 100`` shows growth proportionally
  // regardless of starting balance. Use base = first day's equity so
  // day-0 is exactly 0.00%.
  const equityCurveData = (() => {
    if (daily.length === 0) return [];
    const base = daily[0].equity;
    if (base <= 0) return daily.map((p) => ({ ...p, cum_return_pct: 0 }));
    return daily.map((p) => ({
      ...p,
      cum_return_pct: (p.equity / base - 1) * 100,
    }));
  })();
  const cumReturnSeries = equityCurveData.map((p) => p.cum_return_pct);

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

      {/* Equity curve — cumulative return % over time (Pablo 2026-05-17) */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Equity Curve</CardTitle>
          <CardDescription>
            Cumulative return % from the backtest start
          </CardDescription>
        </CardHeader>
        <CardContent>
          {hasSeries ? (
            <div className="h-72" data-testid="equity-curve-chart">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={equityCurveData}
                  margin={{ top: 4, right: 4, bottom: 0, left: 12 }}
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
                    // % cumulative return — adaptive precision per range.
                    tickFormatter={returnTickFormatter(cumReturnSeries)}
                    // Small-return adaptive padding (cf. equityYDomain).
                    // ``allowDataOverflow`` lets the line draw exactly to
                    // the data extremes instead of being squeezed by tick
                    // rounding when the range is sub-percent.
                    domain={returnYDomain(cumReturnSeries)}
                    allowDataOverflow={false}
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
                      `${(value ?? 0) >= 0 ? "+" : ""}${(value ?? 0).toFixed(3)}%`,
                      "Cum. return",
                    ]}
                  />
                  <Line
                    type="monotone"
                    dataKey="cum_return_pct"
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
                    // drawdown is a ratio (e.g. -0.083 = -8.3%). Same
                    // adaptive-precision pattern as the equity curve so
                    // micro-drawdowns aren't all rendered as "-0.0%".
                    tickFormatter={drawdownTickFormatter(daily)}
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
