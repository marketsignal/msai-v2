"use client";

/**
 * DrawdownBreakdown — table with per-strategy max drawdown + duration.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. Read-only summary:
 * each row is `(strategy_id, max_drawdown_pct, duration_days, recovery)`.
 * When per-strategy attribution is unavailable, the component falls back
 * to showing the portfolio-level row from `metrics.max_drawdown` so the
 * card still carries signal.
 */

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export interface DrawdownRow {
  strategyId: string;
  /** Drawdown as a non-positive fraction (e.g. -0.12 for -12%). */
  maxDrawdown: number;
  /** Duration in days from peak → trough → recovery (or null if never recovered). */
  durationDays: number | null;
  recovered: boolean;
}

export interface DrawdownBreakdownProps {
  rows: DrawdownRow[];
  /** Portfolio-level max drawdown fallback when `rows` is empty. */
  portfolioMaxDrawdown?: number | null;
  testId?: string;
}

function formatPct(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function drawdownTone(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value <= -0.2) return "text-red-500";
  if (value <= -0.1) return "text-amber-500";
  return "text-emerald-500";
}

export function DrawdownBreakdown({
  rows,
  portfolioMaxDrawdown,
  testId = "drawdown-breakdown",
}: DrawdownBreakdownProps): React.ReactElement {
  const hasRows = rows.length > 0;
  const hasPortfolioFallback =
    portfolioMaxDrawdown !== undefined &&
    portfolioMaxDrawdown !== null &&
    Number.isFinite(portfolioMaxDrawdown);

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">Drawdown Breakdown</CardTitle>
        <CardDescription>
          Peak-to-trough decline per strategy with recovery duration
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!hasRows && !hasPortfolioFallback ? (
          <p className="text-sm text-muted-foreground">
            Drawdown breakdown is not available for this run.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Strategy</TableHead>
                <TableHead className="text-right">Max Drawdown</TableHead>
                <TableHead className="text-right">Duration (days)</TableHead>
                <TableHead className="text-right">Recovered</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {hasRows
                ? rows.map((row) => (
                    <TableRow
                      key={row.strategyId}
                      data-testid={`drawdown-row-${row.strategyId}`}
                    >
                      <TableCell className="font-medium">
                        {row.strategyId}
                      </TableCell>
                      <TableCell
                        className={`text-right font-mono tabular-nums ${drawdownTone(row.maxDrawdown)}`}
                      >
                        {formatPct(row.maxDrawdown)}
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums">
                        {row.durationDays ?? "—"}
                      </TableCell>
                      <TableCell className="text-right">
                        {row.recovered ? "Yes" : "No"}
                      </TableCell>
                    </TableRow>
                  ))
                : null}
              {!hasRows && hasPortfolioFallback ? (
                <TableRow data-testid="drawdown-row-portfolio">
                  <TableCell className="font-medium italic">
                    Portfolio (combined)
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${drawdownTone(portfolioMaxDrawdown ?? 0)}`}
                  >
                    {formatPct(portfolioMaxDrawdown ?? 0)}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    —
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">
                    —
                  </TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
