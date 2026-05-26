"use client";

/**
 * MetricsBlock — G5 risk metrics card promoted as primary content on
 * portfolio-run details (`/portfolio/runs/[runId]`).
 *
 * Plan ref: `docs/plans/2026-05-26-ingest-backtest-smoke-test.md` Task 11.
 *
 * Data contract — keys come from
 * `backend/src/msai/services/portfolio/orchestration.py::_enrich_smoke_metrics`:
 *
 *   total_return            (float, fraction — e.g. 0.12 = +12.00%)
 *   pnl                     (float, USD — base_capital * total_return)
 *   sharpe                  (float)
 *   sortino                 (float)
 *   alpha                   (float | null — benchmark alpha)
 *   beta                    (float | null — benchmark beta)
 *   max_drawdown            (float, non-positive fraction — e.g. -0.08)
 *   trade_count_total       (int)
 *   trade_count_by_strategy ({ strategy_name: int })
 *   benchmark_symbol        (str — e.g. "SPY")
 *   smoke_config            (str — "fast" | "nightly", optional)
 *
 * Renders above the existing report-iframe / equity chart so the operator
 * sees the headline numbers BEFORE the rich exploratory content. Falls
 * back to a single "No metrics available for this run." line when
 * `metrics` is null/empty (pending / failed runs).
 */

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export interface MetricsBlockProps {
  metrics: Record<string, unknown> | null;
}

function asNumber(
  metrics: Record<string, unknown> | null,
  key: string,
): number | null {
  if (!metrics) return null;
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asString(
  metrics: Record<string, unknown> | null,
  key: string,
): string | null {
  if (!metrics) return null;
  const value = metrics[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function formatNumber(value: number | null): string {
  if (value === null) return "—";
  return value.toFixed(2);
}

function formatCurrency(value: number | null): string {
  if (value === null) return "—";
  // USD, 2dp, with thousands separators. Negative values render as
  // `-$1,234.56` per Intl default — readable in the Sharpe/Sortino column.
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatInteger(value: number | null): string {
  if (value === null) return "—";
  return Math.round(value).toLocaleString("en-US");
}

function hasAnyG5Key(metrics: Record<string, unknown> | null): boolean {
  if (!metrics) return false;
  const keys = [
    "total_return",
    "pnl",
    "sharpe",
    "sortino",
    "alpha",
    "beta",
    "max_drawdown",
    "trade_count_total",
  ];
  return keys.some((k) => k in metrics);
}

export function MetricsBlock({
  metrics,
}: MetricsBlockProps): React.ReactElement {
  if (!hasAnyG5Key(metrics)) {
    return (
      <Card data-testid="metrics-block">
        <CardHeader>
          <CardTitle className="text-base">Risk metrics</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No metrics available for this run.
          </p>
        </CardContent>
      </Card>
    );
  }

  const benchmark = asString(metrics, "benchmark_symbol") ?? "—";
  const smokeConfig = asString(metrics, "smoke_config");

  const totalReturn = asNumber(metrics, "total_return");
  const pnl = asNumber(metrics, "pnl");
  const sharpe = asNumber(metrics, "sharpe");
  const sortino = asNumber(metrics, "sortino");
  const alpha = asNumber(metrics, "alpha");
  const beta = asNumber(metrics, "beta");
  const maxDrawdown = asNumber(metrics, "max_drawdown");
  const tradesTotal = asNumber(metrics, "trade_count_total");

  // Per-strategy trade counts: `{ strategy_name: int }`. Defensive — drop
  // any non-numeric values so a stale shape can't crash the UI.
  const byStrategyRaw = metrics?.trade_count_by_strategy;
  const byStrategy: Array<[string, number]> = [];
  if (
    byStrategyRaw &&
    typeof byStrategyRaw === "object" &&
    !Array.isArray(byStrategyRaw)
  ) {
    for (const [name, count] of Object.entries(
      byStrategyRaw as Record<string, unknown>,
    )) {
      if (typeof count === "number" && Number.isFinite(count)) {
        byStrategy.push([name, count]);
      }
    }
    byStrategy.sort(([a], [b]) => a.localeCompare(b));
  }

  return (
    <Card data-testid="metrics-block">
      <CardHeader>
        <CardTitle className="text-base">
          Risk metrics (benchmark: {benchmark})
        </CardTitle>
        {smokeConfig !== null ? (
          <CardDescription>
            Smoke config: <code className="font-mono">{smokeConfig}</code>
          </CardDescription>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-6">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Metric
            label="Total return"
            value={formatPercent(totalReturn)}
            testId="metric-total-return"
          />
          <Metric label="P&L" value={formatCurrency(pnl)} testId="metric-pnl" />
          <Metric
            label="Sharpe"
            value={formatNumber(sharpe)}
            testId="metric-sharpe"
          />
          <Metric
            label="Sortino"
            value={formatNumber(sortino)}
            testId="metric-sortino"
          />
          <Metric
            label="Alpha"
            value={formatNumber(alpha)}
            testId="metric-alpha"
          />
          <Metric
            label="Beta"
            value={formatNumber(beta)}
            testId="metric-beta"
          />
          <Metric
            label="Max drawdown"
            value={formatPercent(maxDrawdown)}
            testId="metric-max-drawdown"
          />
          <Metric
            label="Trades total"
            value={formatInteger(tradesTotal)}
            testId="metric-trades-total"
          />
        </dl>

        {byStrategy.length > 0 ? (
          <div className="space-y-2" data-testid="metric-trade-counts">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Trades by strategy
            </p>
            <ul className="grid grid-cols-1 gap-1 text-sm sm:grid-cols-2">
              {byStrategy.map(([name, count]) => (
                <li
                  key={name}
                  className="flex items-baseline justify-between gap-3 border-b border-border/40 py-1 last:border-b-0"
                >
                  <span className="truncate font-mono text-xs text-muted-foreground">
                    {name}
                  </span>
                  <span className="font-medium tabular-nums">
                    {formatInteger(count)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Metric({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}): React.ReactElement {
  return (
    <div className="space-y-1" data-testid={testId}>
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  );
}
