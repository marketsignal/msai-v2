"use client";

/**
 * IsOosPanel — Full-mode 2-column Card showing In-Sample vs Out-of-Sample
 * metrics and the generalization gap (IS - OOS) as a colored Badge.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H6. Renders only when
 * `mode === "full"`; the page-level guard hides this card for Quick runs.
 *
 * The badge tone tracks gap magnitude:
 *   |gap| <= 0.10  → emerald  ("healthy generalization")
 *   |gap| <= 0.30  → amber    ("watch for overfit")
 *   |gap| >  0.30  → red      ("likely overfit — re-run with wider trial space")
 *
 * Thresholds are intentionally conservative — operators can still trust
 * the underlying numbers; the badge is heuristic guidance.
 */

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export interface IsOosPanelProps {
  /** In-sample objective metric (e.g. Sharpe). ``null`` when missing. */
  isMetric: number | null;
  /** Out-of-sample objective metric. ``null`` when missing. */
  oosMetric: number | null;
  testId?: string;
}

function formatMetric(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value.toFixed(3);
}

function gapTone(gap: number | null): {
  variant: "default" | "secondary" | "destructive" | "outline";
  label: string;
  className: string;
} {
  if (gap === null || !Number.isFinite(gap)) {
    return {
      variant: "secondary",
      label: "Unknown",
      className: "bg-muted text-muted-foreground",
    };
  }
  const mag = Math.abs(gap);
  if (mag <= 0.1) {
    return {
      variant: "default",
      label: "Healthy",
      className: "bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25",
    };
  }
  if (mag <= 0.3) {
    return {
      variant: "secondary",
      label: "Watch",
      className: "bg-amber-500/15 text-amber-500 hover:bg-amber-500/25",
    };
  }
  return {
    variant: "destructive",
    label: "Overfit risk",
    className: "bg-red-500/15 text-red-500 hover:bg-red-500/25",
  };
}

export function IsOosPanel({
  isMetric,
  oosMetric,
  testId = "is-oos-panel",
}: IsOosPanelProps): React.ReactElement {
  const gap =
    isMetric !== null &&
    oosMetric !== null &&
    Number.isFinite(isMetric) &&
    Number.isFinite(oosMetric)
      ? isMetric - oosMetric
      : null;

  const tone = gapTone(gap);

  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="text-base">In-Sample vs Out-of-Sample</CardTitle>
        <CardDescription>
          Walk-forward optimizer scores — the gap quantifies overfit risk
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-6">
          <div className="space-y-1" data-testid="is-oos-panel-in-sample">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              In-Sample
            </p>
            <p className="font-mono text-3xl tabular-nums">
              {formatMetric(isMetric)}
            </p>
          </div>
          <div className="space-y-1" data-testid="is-oos-panel-out-of-sample">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Out-of-Sample
            </p>
            <p className="font-mono text-3xl tabular-nums">
              {formatMetric(oosMetric)}
            </p>
          </div>
        </div>
        <div className="mt-6 flex items-center justify-between border-t border-border/40 pt-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Generalization Gap (IS − OOS)
            </p>
            <p className="mt-1 font-mono text-lg tabular-nums">
              {gap === null ? "—" : gap.toFixed(3)}
            </p>
          </div>
          <Badge
            variant={tone.variant}
            className={tone.className}
            data-testid="is-oos-gap-badge"
          >
            {tone.label}
          </Badge>
        </div>
      </CardContent>
    </Card>
  );
}
