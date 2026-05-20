"use client";

/**
 * CorrelationHeatmap — @nivo/heatmap wrapper for an NxN correlation matrix.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H5. Used for both return
 * correlation and drawdown correlation panels on the results page.
 *
 * Diverging red/blue scale pinned at -1..1; the "neutral" midpoint sits at
 * 0 (divergeAt=0.5 maps to the middle of the [-1, 1] domain). Cells render
 * the Pearson coefficient to 2 decimals via `valueFormat=".2f"`.
 */

import { ResponsiveHeatMap } from "@nivo/heatmap";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export type CorrelationMatrix = Record<string, Record<string, number>>;

export interface CorrelationHeatmapProps {
  matrix: CorrelationMatrix;
  title: string;
  description?: string;
  testId: string;
}

export function CorrelationHeatmap({
  matrix,
  title,
  description,
  testId,
}: CorrelationHeatmapProps): React.ReactElement {
  const ids = Object.keys(matrix);

  // Defensive: empty matrix or only one entry → render the empty-state card.
  // A 1x1 self-correlation isn't useful; the page-level renderer also gates
  // on `ids.length >= 2` before mounting, but we double-guard so the
  // component is safe to drop in anywhere.
  if (ids.length < 2) {
    return (
      <Card data-testid={testId}>
        <CardHeader>
          <CardTitle className="text-base">{title}</CardTitle>
          {description ? (
            <CardDescription>{description}</CardDescription>
          ) : null}
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Correlation matrix needs at least 2 strategies — not available for
            this run.
          </p>
        </CardContent>
      </Card>
    );
  }

  const data = ids.map((row) => ({
    id: row,
    data: ids.map((col) => ({ x: col, y: matrix[row]?.[col] ?? 0 })),
  }));

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>
        <div style={{ height: ids.length * 60 + 100 }}>
          <ResponsiveHeatMap
            data={data}
            margin={{ top: 60, right: 30, bottom: 30, left: 90 }}
            valueFormat=".2f"
            axisTop={{
              tickRotation: -30,
              legend: "",
            }}
            axisLeft={{
              tickRotation: 0,
            }}
            colors={{
              type: "diverging",
              scheme: "red_blue",
              divergeAt: 0.5,
              minValue: -1,
              maxValue: 1,
            }}
            emptyColor="#1f1f1f"
            labelTextColor={{
              from: "color",
              modifiers: [["darker", 2.5]],
            }}
            theme={{
              text: { fill: "hsl(0 0% 90%)" },
              axis: {
                ticks: {
                  text: { fill: "hsl(0 0% 70%)", fontSize: 11 },
                },
              },
              tooltip: {
                container: {
                  background: "hsl(0 0% 12.7%)",
                  color: "hsl(0 0% 90%)",
                  fontSize: 12,
                },
              },
            }}
          />
        </div>
      </CardContent>
    </Card>
  );
}
