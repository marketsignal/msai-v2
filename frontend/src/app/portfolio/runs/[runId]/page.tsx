"use client";

/**
 * Portfolio run results — full results page.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` Task H7. Pulls the
 * `PortfolioRunResponse` from `GET /api/v1/portfolios/runs/{id}` and
 * renders:
 *
 *  Always:
 *    - Status + summary header
 *    - Combined equity chart
 *    - Per-strategy contribution (data-not-available path for Quick mode)
 *    - Return correlation heatmap + table (gated on >=2 strategies; the
 *      backend doesn't yet expose per-strategy matrices, so today these
 *      render their empty-state messages — wired in for parity with the
 *      plan + future per-strategy results payload)
 *    - Drawdown correlation heatmap + table (same gating)
 *    - Drawdown breakdown table
 *
 *  Full-mode only (`run.mode === "full"`):
 *    - IS/OOS panel
 *    - Trials table
 *    - Objective scatter
 *
 * Promote-to-Live button POSTs `{account_id}` to
 * `/api/v1/portfolios/runs/{id}/promote-to-live`. v1 hard-codes a default
 * paper account id ("DUTEST123") via a confirm dialog with override input;
 * a richer account picker can land in a follow-up once the IB account
 * inventory endpoint is wired.
 *
 * Route: `/portfolio/runs/[runId]` — chosen over the plan's
 * `/portfolio/[id]/results` so it never collides with a future
 * `/portfolio/[portfolio_id]` detail page (the existing `/portfolio/new`
 * page already redirects there post-create).
 */

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Loader2, Rocket } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { CombinedEquityChart } from "@/components/portfolio-results/combined-equity-chart";
import {
  CorrelationHeatmap,
  type CorrelationMatrix,
} from "@/components/portfolio-results/correlation-heatmap";
import { CorrelationTable } from "@/components/portfolio-results/correlation-table";
import {
  DrawdownBreakdown,
  type DrawdownRow,
} from "@/components/portfolio-results/drawdown-breakdown";
import { IsOosPanel } from "@/components/portfolio-results/is-oos-panel";
import { ObjectiveScatter } from "@/components/portfolio-results/objective-scatter";
import {
  PerStrategyContribution,
  type PerStrategySeries,
} from "@/components/portfolio-results/per-strategy-contribution";
import { TrialsTable } from "@/components/portfolio-results/trials-table";

import {
  apiGet,
  describeApiError,
  promotePortfolioRunToLive,
  type LivePortfolioRevision,
  type PortfolioRunResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime } from "@/lib/format";
import { statusColor } from "@/lib/status";
import { PortfolioStartDialog } from "@/components/live/portfolio-start-dialog";

const DEFAULT_PAPER_ACCOUNT = "DUTEST123";

function metricNumber(
  metrics: Record<string, unknown> | null,
  key: string,
): number | null {
  if (!metrics) return null;
  const value = metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Pull a per-strategy returns/drawdown correlation matrix out of the
 * persisted payload. The backend doesn't ship these on `PortfolioRun` yet
 * (see plan H7 note: "There is NO per-strategy returns data on the
 * response yet"); when they land, the shape will be a top-level dict on
 * `walk_forward_payload` keyed by `return_correlation` /
 * `drawdown_correlation`. Until then this returns `null` so the heatmap
 * + table components render their built-in empty-states.
 */
function extractMatrix(
  payload: Record<string, unknown> | null,
  key: string,
): CorrelationMatrix | null {
  if (!payload) return null;
  const raw = payload[key];
  if (!raw || typeof raw !== "object") return null;
  // Defensive: validate shape rather than blindly cast.
  const out: CorrelationMatrix = {};
  for (const [rowKey, rowVal] of Object.entries(raw)) {
    if (!rowVal || typeof rowVal !== "object") continue;
    const row: Record<string, number> = {};
    for (const [colKey, colVal] of Object.entries(rowVal)) {
      if (typeof colVal === "number" && Number.isFinite(colVal)) {
        row[colKey] = colVal;
      }
    }
    out[rowKey] = row;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/**
 * Extract per-strategy equity slices from `payload.per_strategy_equity`.
 *
 * The backend (Task H+ enrichment in
 * `services/portfolio/orchestration.py::_build_results_payload`) emits a
 * flat record list `[{timestamp, strategy_id, equity}, ...]`. We group
 * by `strategy_id` here so the contribution chart can render one Line
 * per strategy. Older dict-of-arrays shape `{sid: [{timestamp, equity}]}`
 * is also accepted for forward/backward compatibility during the
 * rollout.
 */
function extractPerStrategy(
  payload: Record<string, unknown> | null,
): PerStrategySeries[] | null {
  if (!payload) return null;
  const raw = payload.per_strategy_equity;
  if (!raw) return null;

  // Flat-record shape: [{timestamp, strategy_id, equity}, ...]
  if (Array.isArray(raw)) {
    const grouped: Record<string, Array<{ date: string; equity: number }>> = {};
    for (const row of raw) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      const date = rec.timestamp ?? rec.date;
      const strategyId = rec.strategy_id;
      const equity = rec.equity ?? rec.value;
      if (typeof date !== "string") continue;
      if (typeof strategyId !== "string") continue;
      if (typeof equity !== "number" || !Number.isFinite(equity)) continue;
      (grouped[strategyId] ??= []).push({ date, equity });
    }
    const out: PerStrategySeries[] = Object.entries(grouped)
      .filter(([, points]) => points.length > 0)
      .map(([strategyId, points]) => ({ strategyId, points }));
    return out.length > 0 ? out : null;
  }

  // Legacy dict-of-arrays shape: {sid: [{timestamp, equity}, ...]}
  if (typeof raw !== "object") return null;
  const out: PerStrategySeries[] = [];
  for (const [strategyId, rows] of Object.entries(
    raw as Record<string, unknown>,
  )) {
    if (!Array.isArray(rows)) continue;
    const points: Array<{ date: string; equity: number }> = [];
    for (const row of rows) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      const date = rec.timestamp ?? rec.date;
      const equity = rec.equity ?? rec.value;
      if (typeof date !== "string") continue;
      if (typeof equity !== "number" || !Number.isFinite(equity)) continue;
      points.push({ date, equity });
    }
    if (points.length > 0) out.push({ strategyId, points });
  }
  return out.length > 0 ? out : null;
}

export default function PortfolioRunResultsPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}): React.ReactElement {
  const { runId } = use(params);
  const router = useRouter();
  const { getToken, isAuthenticated } = useAuth();

  const [run, setRun] = useState<PortfolioRunResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState<boolean>(false);

  // Promote-to-Live state
  const [promoteOpen, setPromoteOpen] = useState<boolean>(false);
  const [accountId, setAccountId] = useState<string>(DEFAULT_PAPER_ACCOUNT);
  const [promoting, setPromoting] = useState<boolean>(false);

  // After a successful promote, mount PortfolioStartDialog inline so the
  // user can immediately deploy. Codex-bot PR-73 P2 caught the previous
  // dead-end: the redirect to /live-trading?revision=<id> landed on a
  // page that didn't read the query and PortfolioStartDialog wasn't
  // mounted anywhere — the deployment was stranded.
  const [deployRevision, setDeployRevision] =
    useState<LivePortfolioRevision | null>(null);
  const [deployDialogOpen, setDeployDialogOpen] = useState<boolean>(false);

  useEffect(() => {
    if (!isAuthenticated) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async (): Promise<void> => {
      try {
        const token = await getToken();
        const fresh = await apiGet<PortfolioRunResponse>(
          `/api/v1/portfolios/runs/${encodeURIComponent(runId)}`,
          token,
        );
        if (!active) return;
        setRun(fresh);
        setLoading(false);
        // Keep polling while non-terminal so the user sees Completed when
        // the worker finishes without manual refresh.
        const terminal =
          fresh.status === "completed" ||
          fresh.status === "failed" ||
          fresh.status === "canceled";
        if (!terminal) {
          timer = setTimeout(() => {
            void poll();
          }, 3000);
        }
      } catch (err) {
        if (!active) return;
        // 404 → distinct empty state (typo'd URL); other errors → inline
        // alert with the backend's `describeApiError`-formatted message.
        const status =
          err && typeof err === "object" && "status" in err
            ? (err as { status: unknown }).status
            : null;
        if (status === 404) {
          setNotFound(true);
        } else {
          setError(describeApiError(err, "Failed to load run"));
        }
        setLoading(false);
      }
    };

    void poll();
    return () => {
      active = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [runId, getToken, isAuthenticated]);

  const onPromote = async (): Promise<void> => {
    if (!run || promoting) return;
    if (!accountId.trim()) {
      toast.error("Account id is required.");
      return;
    }
    setPromoting(true);
    try {
      const token = await getToken();
      const result = await promotePortfolioRunToLive(
        run.id,
        { account_id: accountId.trim() },
        token,
      );
      toast.success(
        "Portfolio promoted. Pick deployment options to start trading.",
      );
      setPromoteOpen(false);
      // Hand off to PortfolioStartDialog — the response carries the
      // revision metadata the dialog needs (id + revision_number +
      // composition_hash) so no follow-up fetch is required.
      setDeployRevision({
        id: result.live_portfolio_revision_id,
        revision_number: result.revision_number,
        composition_hash: result.composition_hash,
        is_frozen: true,
        created_at: new Date().toISOString(),
      });
      setDeployDialogOpen(true);
    } catch (err) {
      toast.error(describeApiError(err, "Promote failed"));
    } finally {
      setPromoting(false);
    }
  };

  const returnMatrix = useMemo(
    () =>
      extractMatrix(run?.walk_forward_payload ?? null, "return_correlation"),
    [run],
  );
  const drawdownMatrix = useMemo(
    () =>
      extractMatrix(run?.walk_forward_payload ?? null, "drawdown_correlation"),
    [run],
  );
  const perStrategy = useMemo(
    () => extractPerStrategy(run?.walk_forward_payload ?? null),
    [run],
  );
  /**
   * Per-strategy error attribution (PRD US-002a).  The worker persists a
   * structured list under ``run.metrics.per_strategy_errors`` when a
   * Quick-mode member raises during ``_execute_candidate_backtests``.
   * Each entry: ``{strategy_id, strategy_name, candidate_id, error_type,
   * message}``.  Defensive extractor — silently drops malformed rows so
   * a stale UI never crashes on an unexpected metric shape.
   */
  const perStrategyErrors = useMemo<
    Array<{
      strategy_id: string;
      strategy_name: string;
      error_type: string;
      message: string;
    }>
  >(() => {
    const metrics = run?.metrics;
    if (!metrics || typeof metrics !== "object") return [];
    const raw = (metrics as Record<string, unknown>).per_strategy_errors;
    if (!Array.isArray(raw)) return [];
    const out: Array<{
      strategy_id: string;
      strategy_name: string;
      error_type: string;
      message: string;
    }> = [];
    for (const row of raw) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      out.push({
        strategy_id: typeof rec.strategy_id === "string" ? rec.strategy_id : "",
        strategy_name:
          typeof rec.strategy_name === "string" ? rec.strategy_name : "",
        error_type: typeof rec.error_type === "string" ? rec.error_type : "",
        message: typeof rec.message === "string" ? rec.message : "",
      });
    }
    return out;
  }, [run]);

  const drawdownRows: DrawdownRow[] = useMemo(() => {
    // The Task H+ backend enrichment serialises per-strategy drawdown
    // summary stats under `drawdown_breakdown` keyed by strategy_id —
    // `{sid: {max_drawdown, duration_days, recovered}}`. The legacy
    // `per_strategy_drawdown` key holds the flat time-series (used by
    // future per-strategy underwater charts); fall back to it as a
    // last resort in case the rollout staggers the two consumers.
    const payload = run?.walk_forward_payload;
    const raw =
      (payload?.drawdown_breakdown as unknown) ??
      payload?.per_strategy_drawdown;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const out: DrawdownRow[] = [];
    for (const [strategyId, row] of Object.entries(
      raw as Record<string, unknown>,
    )) {
      if (!row || typeof row !== "object") continue;
      const rec = row as Record<string, unknown>;
      const mdd = rec.max_drawdown;
      const dur = rec.duration_days;
      const rec_ok = rec.recovered;
      out.push({
        strategyId,
        maxDrawdown: typeof mdd === "number" ? mdd : 0,
        durationDays: typeof dur === "number" ? dur : null,
        recovered: rec_ok === true,
      });
    }
    return out;
  }, [run]);

  if (!isAuthenticated || loading) {
    return (
      <div className="container mx-auto flex h-[60vh] items-center justify-center">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="container mx-auto max-w-3xl space-y-4 py-8">
        <Link
          href="/portfolio"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to portfolios
        </Link>
        <Card>
          <CardHeader>
            <CardTitle>Run not found</CardTitle>
            <CardDescription>
              We could not find portfolio run{" "}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                {runId}
              </code>
              . It may have been deleted, or the URL may be malformed.
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    );
  }

  if (error || !run) {
    return (
      <div className="container mx-auto max-w-3xl space-y-4 py-8">
        <Link
          href="/portfolio"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" />
          Back to portfolios
        </Link>
        <Card>
          <CardHeader>
            <CardTitle>Could not load run</CardTitle>
            <CardDescription className="text-destructive">
              {error ?? "Unknown error"}
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    );
  }

  const isFull = run.mode === "full";
  const isCompleted = run.status === "completed";
  const portfolioMdd = metricNumber(run.metrics, "max_drawdown");

  return (
    <div className="container mx-auto max-w-6xl space-y-6 py-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1.5">
          <Link
            href="/portfolio"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-3.5" />
            Back to portfolios
          </Link>
          <h1 className="text-3xl font-bold tracking-tight">
            Portfolio Run Results
          </h1>
          <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <Badge className={statusColor(run.status)}>{run.status}</Badge>
            <Badge variant="outline" className="uppercase">
              {run.mode}
            </Badge>
            <span>
              {run.start_date} → {run.end_date}
            </span>
            {run.completed_at ? (
              <span>· completed {formatDateTime(run.completed_at)}</span>
            ) : null}
          </div>
        </div>

        <Dialog open={promoteOpen} onOpenChange={setPromoteOpen}>
          <DialogTrigger asChild>
            <Button
              data-testid="promote-to-live"
              disabled={!isCompleted}
              variant="default"
              className="gap-2"
            >
              <Rocket className="size-4" />
              Deploy as Live Portfolio
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Promote to Live (Paper)</DialogTitle>
              <DialogDescription>
                Phase 1 promote-to-live is paper-only — the account id must
                start with <code>DU</code>. The risk engine validates the
                composition before the revision is created.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2 py-2">
              <Label htmlFor="promote-account">IB paper account id</Label>
              <Input
                id="promote-account"
                data-testid="promote-account-input"
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
                placeholder="DUTEST123"
              />
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setPromoteOpen(false)}
                disabled={promoting}
              >
                Cancel
              </Button>
              <Button
                data-testid="promote-to-live-confirm"
                disabled={promoting}
                onClick={() => void onPromote()}
              >
                {promoting ? (
                  <>
                    <Loader2 className="mr-2 size-4 animate-spin" />
                    Promoting...
                  </>
                ) : (
                  "Promote"
                )}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Deploy dialog — mounted after a successful promote. Codex-bot
            PR-73 P2 caught the previous dead-end where the promote redirect
            stranded the user on /live-trading with no way to start the
            deployment. The dialog handles the 4-stage flow (form → preview
            → confirm → submit) against POST /api/v1/live/start-portfolio. */}
        {deployRevision !== null ? (
          <PortfolioStartDialog
            revision={deployRevision}
            open={deployDialogOpen}
            onOpenChange={(next) => {
              setDeployDialogOpen(next);
              if (!next) {
                setDeployRevision(null);
              }
            }}
            onSuccess={() => {
              setDeployDialogOpen(false);
              setDeployRevision(null);
              toast.success("Deployment started.");
              router.push("/live-trading");
            }}
          />
        ) : null}
      </div>

      {!isCompleted ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Run is {run.status}</CardTitle>
            <CardDescription>
              Charts and the promote button populate when the run completes.
              {run.status === "running" || run.status === "pending"
                ? " This page polls automatically."
                : null}
            </CardDescription>
          </CardHeader>
          {run.error_message ? (
            <CardContent>
              <p className="text-sm text-destructive">{run.error_message}</p>
            </CardContent>
          ) : null}
        </Card>
      ) : null}

      {/* Per-strategy error attribution (Quick-mode failures only).
       *  PRD US-002a — when one member raises during a Quick-mode run, the
       *  worker persists the structured error block on ``metrics`` so the
       *  operator can immediately see WHICH strategy failed. The block is
       *  intentionally Quick-only because Full-mode trials are sampled
       *  across strategies and don't surface per-member failures the same
       *  way (a single bad trial doesn't fail the run). */}
      {run.mode === "quick" &&
      Array.isArray(perStrategyErrors) &&
      perStrategyErrors.length > 0 ? (
        <Card data-testid="per-strategy-errors">
          <CardHeader>
            <CardTitle className="text-base">Per-strategy errors</CardTitle>
            <CardDescription>
              The following member strategies raised during the run; the
              portfolio failed before metrics could be computed.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {perStrategyErrors.map((err, idx) => (
                <li
                  key={`${err.strategy_id || idx}`}
                  className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
                >
                  <div className="font-medium text-foreground">
                    {err.strategy_name || err.strategy_id || "<unknown>"}{" "}
                    <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-xs font-mono text-muted-foreground">
                      {err.error_type}
                    </span>
                  </div>
                  <p className="mt-1 text-muted-foreground">{err.message}</p>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <CombinedEquityChart series={run.series} />

      <PerStrategyContribution perStrategy={perStrategy} />

      <div className="grid gap-6 lg:grid-cols-2">
        <CorrelationHeatmap
          matrix={returnMatrix ?? {}}
          title="Return Correlation"
          description="Pearson correlation of strategy return series"
          testId="return-correlation-heatmap"
        />
        <CorrelationTable
          matrix={returnMatrix ?? {}}
          title="Return Correlation — Table"
          description="Sortable view of the same matrix"
          testId="return-correlation-table"
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <CorrelationHeatmap
          matrix={drawdownMatrix ?? {}}
          title="Drawdown Correlation"
          description="Real-diversification signal — returns can decorrelate while drawdowns still align"
          testId="drawdown-correlation-heatmap"
        />
        <CorrelationTable
          matrix={drawdownMatrix ?? {}}
          title="Drawdown Correlation — Table"
          description="Sortable view of the drawdown matrix"
          testId="drawdown-correlation-table"
        />
      </div>

      <DrawdownBreakdown
        rows={drawdownRows}
        portfolioMaxDrawdown={portfolioMdd}
      />

      {isFull ? (
        <>
          <IsOosPanel isMetric={run.is_metric} oosMetric={run.oos_metric} />
          <TrialsTable trials={run.optimization_trace} />
          <ObjectiveScatter trials={run.optimization_trace} />
        </>
      ) : null}
    </div>
  );
}
