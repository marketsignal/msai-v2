"use client";

/**
 * PerStrategyContribution — stacked AreaChart showing each strategy's
 * contribution to combined portfolio equity over time.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. The backend's Quick
 * mode persists the combined `series` only (not per-strategy slices); when
 * the optional ``perStrategy`` prop is missing we render an explanatory
 * empty-state instead of a misleading single-color stack. This is the
 * "data not available" path the H7 task description anticipates.
 */

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
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

export interface PerStrategySeries {
  /** Strategy display name / id used as the stack key + legend label. */
  strategyId: string;
  /** Equity points for this strategy, parallel to the combined series. */
  points: Array<{ date: string; equity: number }>;
}

export interface PerStrategyContributionProps {
  /**
   * Per-strategy equity slices. When ``null`` or empty, the component
   * renders an explanatory empty-state — currently the API doesn't ship
   * per-strategy data on `PortfolioRunResponse`, so this is the path used
   * for every Quick-mode run as of plan H6.
   */
  perStrategy: PerStrategySeries[] | null;
  testId?: string;
}

// Tailwind-friendly palette: distinct hues, all accessible against the
// dark surface used by Card. Cycled by index so adding strategies just
// wraps rather than crashing.
const STACK_COLORS = [
  "hsl(142, 76%, 36%)",
  "hsl(217, 91%, 60%)",
  "hsl(38, 92%, 50%)",
  "hsl(280, 70%, 60%)",
  "hsl(0, 84%, 60%)",
  "hsl(178, 60%, 45%)",
  "hsl(48, 96%, 53%)",
  "hsl(316, 70%, 55%)",
];

interface ChartRow {
  date: string;
  [strategyId: string]: string | number;
}

function buildStackedRows(slices: PerStrategySeries[]): ChartRow[] {
  // Merge by date — assume every strategy publishes the same date axis
  // (per-strategy attribution comes from the same bar timeline). Missing
  // dates contribute 0 so the stack stays well-formed.
  const dateIndex = new Map<string, ChartRow>();
  for (const slice of slices) {
    for (const point of slice.points) {
      const row = dateIndex.get(point.date) ?? { date: point.date };
      row[slice.strategyId] = point.equity;
      dateIndex.set(point.date, row);
    }
  }
  return [...dateIndex.values()].sort((a, b) =>
    a.date < b.date ? -1 : a.date > b.date ? 1 : 0,
  );
}

export function PerStrategyContribution({
  perStrategy,
  testId = "per-strategy-contribution",
}: PerStrategyContributionProps): React.ReactElement {
  const slices = perStrategy ?? [];
  const hasData = slices.length > 0 && slices.some((s) => s.points.length > 0);

  return (
    <Card data-testid={testId} className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Per-Strategy Contribution</CardTitle>
        <CardDescription>
          Stacked equity contribution by member strategy
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!hasData ? (
          <div className="flex h-48 items-center justify-center text-center text-sm text-muted-foreground">
            Per-strategy attribution is not exposed on this run yet.
            <br />
            Combined equity is available above.
          </div>
        ) : (
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={buildStackedRows(slices)}
                margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
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
                    if (Number.isNaN(d.getTime())) return v;
                    return `${d.getMonth() + 1}/${d.getDate()}`;
                  }}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "hsl(0 0% 12.7%)",
                    border: "1px solid hsl(0 0% 100% / 0.1)",
                    borderRadius: "8px",
                    fontSize: "12px",
                  }}
                  labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                  formatter={(
                    value: number | undefined,
                    name: string | undefined,
                  ) => [formatCurrency(value ?? 0), name ?? ""]}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {slices.map((slice, i) => (
                  <Area
                    key={slice.strategyId}
                    type="monotone"
                    dataKey={slice.strategyId}
                    stackId="contribution"
                    stroke={STACK_COLORS[i % STACK_COLORS.length]}
                    fill={STACK_COLORS[i % STACK_COLORS.length]}
                    fillOpacity={0.35}
                    strokeWidth={1.5}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
