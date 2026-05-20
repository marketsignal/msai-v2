"use client";

/**
 * CombinedEquityChart — combined-portfolio equity curve (Recharts AreaChart).
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. Uses Recharts rather
 * than TradingView Lightweight Charts to stay consistent with the existing
 * `<EquityChart>` (dashboard/equity-chart.tsx) — both render the same
 * "value over time" shape with the same color/gradient idiom.
 *
 * Input ``series`` is the raw `PortfolioRun.series` payload from the API:
 * a list of dicts each containing at least ``timestamp`` and ``equity``.
 * We coerce + filter defensively because the backend persists this as
 * `list[dict[str, Any]]` (no Pydantic shape guarantee for inner records).
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatCurrency } from "@/lib/format";

export interface CombinedEquityChartProps {
  /**
   * Raw API series — each entry should contain ``timestamp`` (ISO string)
   * and ``equity`` (number). Extra keys are ignored. ``null`` renders the
   * empty-state.
   */
  series: Array<Record<string, unknown>> | null;
  testId?: string;
}

interface Point {
  date: string;
  equity: number;
}

function normalizeSeries(raw: Array<Record<string, unknown>> | null): Point[] {
  if (!raw) return [];
  const out: Point[] = [];
  for (const row of raw) {
    const ts = row.timestamp ?? row.date;
    const eq = row.equity ?? row.value;
    if (typeof ts !== "string") continue;
    if (typeof eq !== "number" || !Number.isFinite(eq)) continue;
    out.push({ date: ts, equity: eq });
  }
  return out;
}

export function CombinedEquityChart({
  series,
  testId = "combined-equity-chart",
}: CombinedEquityChartProps): React.ReactElement {
  const data = normalizeSeries(series);

  return (
    <Card data-testid={testId} className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Combined Equity</CardTitle>
        <CardDescription>
          Portfolio-level equity across all member strategies
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
            No equity data available for this run.
          </div>
        ) : (
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={data}
                margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
              >
                <defs>
                  <linearGradient
                    id="portfolioEquityGradient"
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop
                      offset="0%"
                      stopColor="hsl(142, 76%, 36%)"
                      stopOpacity={0.35}
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
                    if (Number.isNaN(d.getTime())) return v;
                    return `${d.getMonth() + 1}/${d.getDate()}`;
                  }}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                  domain={["dataMin", "dataMax"]}
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
                <Area
                  type="monotone"
                  dataKey="equity"
                  stroke="hsl(142, 76%, 36%)"
                  strokeWidth={2}
                  fill="url(#portfolioEquityGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
