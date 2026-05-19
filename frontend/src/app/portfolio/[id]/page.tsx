"use client";

/**
 * Portfolio composition detail page.
 *
 * Plan ref: `docs/plans/portfolio-backtest.md` US-002a Quick scenario —
 * "Given I am at /portfolios/<id> with a saved composition / When I click
 * 'Run Backtest' / And I select Mode = 'Quick' ...". Resolves the
 * verify-e2e iter-2 finding that /portfolio/{id} returned 404 after the
 * compose form push.
 *
 * Surfaces:
 *  - Composition summary (name, allocator, objective, safety caps,
 *    member strategies)
 *  - Run Backtest action with Quick / Full mode toggle
 *  - Past runs of this portfolio, link out to /portfolio/runs/[runId]
 *
 * The page is **read-only** for composition — editing would create a new
 * revision (deferred; see plan § Tradeoffs Accepted).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Briefcase, Play, ExternalLink, AlertTriangle } from "lucide-react";

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
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import {
  apiGet,
  apiPost,
  ApiError,
  describeApiError,
  type PortfolioResponse,
  type PortfolioRunListResponse,
  type PortfolioRunResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime, formatCurrency } from "@/lib/format";
import { statusColor } from "@/lib/status";

interface PortfolioDetailPageProps {
  readonly params: Promise<{ readonly id: string }>;
}

const DEFAULT_START = "2024-01-02";
const DEFAULT_END = "2024-12-31";

export default function PortfolioDetailPage({
  params,
}: PortfolioDetailPageProps): React.JSX.Element {
  const router = useRouter();
  const { getToken } = useAuth();

  const [portfolioId, setPortfolioId] = useState<string | null>(null);
  const [portfolio, setPortfolio] = useState<PortfolioResponse | null>(null);
  const [runs, setRuns] = useState<readonly PortfolioRunResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);

  // Run-Backtest modal state
  const [runDialogOpen, setRunDialogOpen] = useState<boolean>(false);
  const [runMode, setRunMode] = useState<"quick" | "full">("quick");
  const [runStart, setRunStart] = useState<string>(DEFAULT_START);
  const [runEnd, setRunEnd] = useState<string>(DEFAULT_END);
  const [runNTrials, setRunNTrials] = useState<string>("");
  const [submittingRun, setSubmittingRun] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  useEffect(() => {
    void params.then((p) => {
      setPortfolioId(p.id);
    });
  }, [params]);

  const loadAll = useCallback(async (): Promise<void> => {
    if (!portfolioId) return;
    setLoading(true);
    setError(null);
    setNotFound(false);
    try {
      const token = await getToken();
      const detail = await apiGet<PortfolioResponse>(
        `/api/v1/portfolios/${portfolioId}`,
        token,
      );
      setPortfolio(detail);
      const runsResp = await apiGet<PortfolioRunListResponse>(
        `/api/v1/portfolios/runs?portfolio_id=${portfolioId}`,
        token,
      );
      setRuns(runsResp.items ?? []);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
      } else {
        setError(describeApiError(err, "Could not load portfolio"));
      }
    } finally {
      setLoading(false);
    }
  }, [portfolioId, getToken]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const onSubmitRun = useCallback(async (): Promise<void> => {
    if (!portfolio) return;
    setSubmittingRun(true);
    setRunError(null);
    try {
      const token = await getToken();
      const body: Record<string, unknown> = {
        start_date: runStart,
        end_date: runEnd,
        mode: runMode,
      };
      if (runMode === "full" && runNTrials.trim().length > 0) {
        const parsed = Number.parseInt(runNTrials.trim(), 10);
        if (Number.isFinite(parsed) && parsed > 0) {
          body.n_trials = parsed;
        }
      }
      const created = await apiPost<PortfolioRunResponse>(
        `/api/v1/portfolios/${portfolio.id}/runs`,
        body,
        token,
      );
      setRunDialogOpen(false);
      router.push(`/portfolio/runs/${created.id}`);
    } catch (err) {
      setRunError(describeApiError(err, "Could not start backtest"));
    } finally {
      setSubmittingRun(false);
    }
  }, [portfolio, runStart, runEnd, runMode, runNTrials, getToken, router]);

  const sortedRuns = useMemo(() => {
    return [...runs].sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [runs]);

  if (loading && !portfolio && !notFound && !error) {
    return (
      <div
        className="container mx-auto py-8"
        data-testid="portfolio-detail-loading"
      >
        <p className="text-muted-foreground">Loading portfolio…</p>
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="container mx-auto py-8 space-y-4">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-500" />
              Portfolio not found
            </CardTitle>
            <CardDescription>
              No portfolio with id {portfolioId}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link href="/portfolio">
              <Button variant="outline">Back to portfolios</Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error) {
    return (
      <div className="container mx-auto py-8 space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Could not load portfolio</CardTitle>
            <CardDescription>{error}</CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={() => void loadAll()} variant="outline">
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!portfolio) {
    return <div className="container mx-auto py-8" />;
  }

  return (
    <div
      className="container mx-auto py-8 space-y-6 max-w-4xl"
      data-testid="portfolio-detail"
    >
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <Briefcase className="h-6 w-6 text-muted-foreground" />
            <h1 className="text-3xl font-bold">{portfolio.name}</h1>
          </div>
          {portfolio.description ? (
            <p className="text-muted-foreground max-w-2xl">
              {portfolio.description}
            </p>
          ) : null}
        </div>
        <Button
          data-testid="run-backtest-button"
          onClick={() => setRunDialogOpen(true)}
          className="shrink-0"
        >
          <Play className="h-4 w-4 mr-2" />
          Run Backtest
        </Button>
      </header>

      <Card data-testid="composition-summary">
        <CardHeader>
          <CardTitle>Composition</CardTitle>
          <CardDescription>
            Read-only summary. Edits create a new revision (coming in v2).
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-4 text-sm">
          <SummaryField label="Objective" value={portfolio.objective} />
          <SummaryField
            label="Allocator"
            value={portfolio.allocator_name ?? "—"}
          />
          <SummaryField
            label="Default mode"
            value={portfolio.default_mode ?? "quick"}
          />
          <SummaryField
            label="Initial capital"
            value={formatCurrency(portfolio.base_capital)}
          />
          <SummaryField
            label="Requested leverage"
            value={`${portfolio.requested_leverage.toFixed(2)}×`}
          />
          <SummaryField
            label="Downside target"
            value={
              portfolio.downside_target != null
                ? portfolio.downside_target.toFixed(2)
                : "—"
            }
          />
          <SummaryField
            label="Max position size"
            value={
              portfolio.max_position_size != null
                ? `${(portfolio.max_position_size * 100).toFixed(0)}%`
                : "—"
            }
          />
          <SummaryField
            label="Max drawdown halt"
            value={
              portfolio.max_drawdown_halt != null
                ? `${(portfolio.max_drawdown_halt * 100).toFixed(0)}%`
                : "—"
            }
          />
          <SummaryField
            label="Created"
            value={formatDateTime(portfolio.created_at)}
          />
        </CardContent>
      </Card>

      <Card data-testid="runs-table-card">
        <CardHeader>
          <CardTitle>Backtest runs</CardTitle>
          <CardDescription>
            {sortedRuns.length} run{sortedRuns.length === 1 ? "" : "s"} for this
            portfolio
          </CardDescription>
        </CardHeader>
        <CardContent>
          {sortedRuns.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No runs yet. Click <strong>Run Backtest</strong> to start.
            </p>
          ) : (
            <Table data-testid="runs-table">
              <TableHeader>
                <TableRow>
                  <TableHead>Created</TableHead>
                  <TableHead>Mode</TableHead>
                  <TableHead>Dates</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Open</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedRuns.map((run) => (
                  <TableRow key={run.id} data-testid={`run-row-${run.id}`}>
                    <TableCell>{formatDateTime(run.created_at)}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="uppercase">
                        {run.mode ?? "quick"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {run.start_date} → {run.end_date}
                    </TableCell>
                    <TableCell>
                      <Badge className={statusColor(run.status)}>
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Link href={`/portfolio/runs/${run.id}`}>
                        <Button variant="ghost" size="sm">
                          <ExternalLink className="h-4 w-4" />
                        </Button>
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={runDialogOpen} onOpenChange={setRunDialogOpen}>
        <DialogContent data-testid="run-dialog">
          <DialogHeader>
            <DialogTitle>Run Backtest</DialogTitle>
            <DialogDescription>
              Pick a mode and date range, then start the run.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label htmlFor="run-mode">Mode</Label>
              <Select
                value={runMode}
                onValueChange={(value) => setRunMode(value as "quick" | "full")}
              >
                <SelectTrigger id="run-mode" data-testid="run-mode-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="quick">
                    Quick (single-shot, ~5 min)
                  </SelectItem>
                  <SelectItem value="full">
                    Full (optimization + walk-forward, up to 8h)
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <Label htmlFor="run-start">Start date</Label>
                <Input
                  id="run-start"
                  type="date"
                  value={runStart}
                  onChange={(e) => setRunStart(e.target.value)}
                  data-testid="run-start-date"
                />
              </div>
              <div>
                <Label htmlFor="run-end">End date</Label>
                <Input
                  id="run-end"
                  type="date"
                  value={runEnd}
                  onChange={(e) => setRunEnd(e.target.value)}
                  data-testid="run-end-date"
                />
              </div>
            </div>
            {runMode === "full" ? (
              <div>
                <Label htmlFor="run-ntrials">Trials (override)</Label>
                <Input
                  id="run-ntrials"
                  type="number"
                  min={1}
                  max={1000}
                  placeholder="(default 100; pick 2-10 for smoke runs)"
                  value={runNTrials}
                  onChange={(e) => setRunNTrials(e.target.value)}
                  data-testid="run-ntrials"
                />
                <p className="text-xs text-muted-foreground mt-1">
                  Lower trial counts finish faster but explore less of the
                  parameter space.
                </p>
              </div>
            ) : null}
            {runError ? (
              <p className="text-sm text-destructive">{runError}</p>
            ) : null}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRunDialogOpen(false)}
              disabled={submittingRun}
            >
              Cancel
            </Button>
            <Button
              onClick={() => void onSubmitRun()}
              disabled={submittingRun}
              data-testid="run-submit"
            >
              {submittingRun ? "Starting…" : "Start run"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function SummaryField({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}): React.JSX.Element {
  return (
    <div className="space-y-1">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="font-medium">{value}</div>
    </div>
  );
}
