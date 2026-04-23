import { AlertTriangle, Info } from "lucide-react";
import type { SeriesStatus } from "@/lib/api";

interface SeriesStatusIndicatorProps {
  status: SeriesStatus;
}

/**
 * Shared empty-state strip for the four chart surfaces when the canonical
 * ``Backtest.series`` payload is unavailable. Renders one of two visuals:
 *
 * - ``not_materialized`` — informational (this backtest predates the
 *   charts rollout). Neutral color, no alert.
 * - ``failed`` — amber warning; aggregate metrics above the chart remain
 *   valid because the worker's fail-soft path writes ``series=None`` +
 *   ``series_status="failed"`` without failing the backtest itself
 *   (PRD US-006).
 *
 * When ``status === "ready"`` the parent gates this component out (charts
 * render real data instead), so the component returns a fragment.
 */
export function SeriesStatusIndicator({
  status,
}: SeriesStatusIndicatorProps): React.JSX.Element {
  if (status === "not_materialized") {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 py-10 text-muted-foreground"
        data-testid="series-status-not-materialized"
      >
        <Info className="h-6 w-6" aria-hidden="true" />
        <p className="text-sm">Analytics unavailable for this backtest.</p>
        <p className="text-xs">Re-run the backtest to populate charts.</p>
      </div>
    );
  }

  if (status === "failed") {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 py-10 text-amber-500"
        data-testid="series-status-failed"
      >
        <AlertTriangle className="h-6 w-6" aria-hidden="true" />
        <p className="text-sm">Analytics computation failed.</p>
        <p className="text-xs text-muted-foreground">
          Aggregate metrics above are still valid. Try re-running the backtest.
        </p>
      </div>
    );
  }

  // "ready" — never rendered (parent gates).
  return <></>;
}
