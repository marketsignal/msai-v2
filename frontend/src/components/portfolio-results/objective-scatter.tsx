"use client";

/**
 * ObjectiveScatter — Recharts ScatterChart showing trial objective values
 * across the optimization search.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. X-axis = trial
 * index (chronological order in the trace), Y-axis = score. The shape of
 * the cloud tells the operator whether the optimizer converged (clustered
 * scores at the top right) or thrashed (uniform scatter).
 */

import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
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

export interface ObjectiveScatterProps {
  /** Raw `PortfolioRun.optimization_trace` payload. ``null`` for Quick. */
  trials: Array<Record<string, unknown>> | null;
  testId?: string;
}

interface ScatterPoint {
  index: number;
  score: number;
}

function normalizePoints(
  raw: Array<Record<string, unknown>> | null,
): ScatterPoint[] {
  if (!raw) return [];
  const out: ScatterPoint[] = [];
  raw.forEach((row, i) => {
    const score = row.score ?? row.objective_value ?? row.value;
    if (typeof score === "number" && Number.isFinite(score)) {
      out.push({ index: i, score });
    }
  });
  return out;
}

export function ObjectiveScatter({
  trials,
  testId = "objective-scatter",
}: ObjectiveScatterProps): React.ReactElement {
  const points = normalizePoints(trials);

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">Objective Scatter</CardTitle>
        <CardDescription>
          Trial score by index — convergence shows as clustered high scores
        </CardDescription>
      </CardHeader>
      <CardContent>
        {points.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
            No trial scores recorded for this run.
          </div>
        ) : (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(0 0% 50% / 0.1)"
                />
                <XAxis
                  type="number"
                  dataKey="index"
                  name="Trial"
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  label={{
                    value: "Trial index",
                    position: "insideBottom",
                    offset: -2,
                    fill: "hsl(0 0% 63.9%)",
                    fontSize: 11,
                  }}
                />
                <YAxis
                  type="number"
                  dataKey="score"
                  name="Score"
                  tick={{ fontSize: 11, fill: "hsl(0 0% 63.9%)" }}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => v.toFixed(2)}
                  domain={["auto", "auto"]}
                />
                <Tooltip
                  cursor={{
                    strokeDasharray: "3 3",
                    stroke: "hsl(0 0% 50% / 0.4)",
                  }}
                  contentStyle={{
                    backgroundColor: "hsl(0 0% 12.7%)",
                    border: "1px solid hsl(0 0% 100% / 0.1)",
                    borderRadius: "8px",
                    fontSize: "12px",
                  }}
                  labelStyle={{ color: "hsl(0 0% 63.9%)" }}
                  formatter={(value: number | undefined) =>
                    (value ?? 0).toFixed(4)
                  }
                />
                <Scatter
                  data={points}
                  fill="hsl(217, 91%, 60%)"
                  fillOpacity={0.8}
                />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
