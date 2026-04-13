"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PieChart, Plus, Play, Trash2, Loader2, Briefcase } from "lucide-react";
import {
  apiGet,
  apiPost,
  ApiError,
  type PortfolioResponse,
  type PortfolioListResponse,
  type PortfolioRunResponse,
  type PortfolioRunListResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatDateTime, formatCurrency } from "@/lib/format";
import { statusColor } from "@/lib/status";

function objectiveLabel(objective: string): string {
  switch (objective) {
    case "maximize_sharpe":
      return "Max Sharpe";
    case "equal_weight":
      return "Equal Weight";
    case "manual":
      return "Manual";
    default:
      return objective;
  }
}

function objectiveColor(objective: string): string {
  switch (objective) {
    case "maximize_sharpe":
      return "bg-violet-500/15 text-violet-500";
    case "equal_weight":
      return "bg-sky-500/15 text-sky-500";
    case "manual":
      return "bg-zinc-500/15 text-zinc-400";
    default:
      return "bg-muted text-muted-foreground";
  }
}

// ---------------------------------------------------------------------------
// Allocation row type
// ---------------------------------------------------------------------------

interface AllocationRow {
  candidate_id: string;
  weight: string;
}

// ---------------------------------------------------------------------------
// Create Portfolio Dialog
// ---------------------------------------------------------------------------

interface CreatePortfolioDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
}

function CreatePortfolioDialog({
  open,
  onOpenChange,
  onCreated,
}: CreatePortfolioDialogProps): React.ReactElement {
  const { getToken } = useAuth();
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [name, setName] = useState<string>("");
  const [description, setDescription] = useState<string>("");
  const [objective, setObjective] = useState<string>("maximize_sharpe");
  const [baseCapital, setBaseCapital] = useState<string>("100000");
  const [leverage, setLeverage] = useState<string>("1.0");
  const [benchmark, setBenchmark] = useState<string>("");
  const [allocations, setAllocations] = useState<AllocationRow[]>([]);

  const resetForm = useCallback((): void => {
    setName("");
    setDescription("");
    setObjective("maximize_sharpe");
    setBaseCapital("100000");
    setLeverage("1.0");
    setBenchmark("");
    setAllocations([]);
    setFormError(null);
  }, []);

  const addAllocation = (): void => {
    setAllocations((prev) => [...prev, { candidate_id: "", weight: "0.5" }]);
  };

  const removeAllocation = (idx: number): void => {
    setAllocations((prev) => prev.filter((_, i) => i !== idx));
  };

  const updateAllocation = (
    idx: number,
    field: keyof AllocationRow,
    value: string,
  ): void => {
    setAllocations((prev) =>
      prev.map((row, i) => (i === idx ? { ...row, [field]: value } : row)),
    );
  };

  const handleSubmit = async (): Promise<void> => {
    if (!name.trim()) {
      setFormError("Name is required.");
      return;
    }
    const capital = parseFloat(baseCapital);
    const lev = parseFloat(leverage);
    if (isNaN(capital) || capital <= 0) {
      setFormError("Base capital must be a positive number.");
      return;
    }
    if (isNaN(lev) || lev <= 0) {
      setFormError("Leverage must be a positive number.");
      return;
    }

    const parsedAllocations = allocations
      .filter((a) => a.candidate_id.trim())
      .map((a) => ({
        candidate_id: a.candidate_id.trim(),
        weight: parseFloat(a.weight) || 0,
      }));

    setSubmitting(true);
    setFormError(null);
    try {
      const token = await getToken();
      await apiPost<PortfolioResponse>(
        "/api/v1/portfolios",
        {
          name: name.trim(),
          description: description.trim() || null,
          objective,
          base_capital: capital,
          requested_leverage: lev,
          benchmark_symbol: benchmark.trim() || null,
          allocations: parsedAllocations,
        },
        token,
      );
      resetForm();
      onOpenChange(false);
      onCreated();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to create portfolio (${err.status})`
          : "Failed to create portfolio";
      setFormError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) resetForm();
        onOpenChange(v);
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm">
          <Plus className="mr-1.5 size-3.5" />
          Create Portfolio
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Portfolio</DialogTitle>
          <DialogDescription>
            Define a weighted strategy allocation with backtest configuration.
          </DialogDescription>
        </DialogHeader>

        {formError && (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
            {formError}
          </div>
        )}

        <div className="space-y-4">
          {/* Name */}
          <div className="space-y-1.5">
            <Label htmlFor="pf-name">Name</Label>
            <Input
              id="pf-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Momentum Basket Q2"
            />
          </div>

          {/* Description */}
          <div className="space-y-1.5">
            <Label htmlFor="pf-desc">Description (optional)</Label>
            <Textarea
              id="pf-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Portfolio rationale..."
              rows={2}
              className="resize-none"
            />
          </div>

          {/* Objective */}
          <div className="space-y-1.5">
            <Label htmlFor="pf-objective">Objective</Label>
            <Select value={objective} onValueChange={setObjective}>
              <SelectTrigger id="pf-objective">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="maximize_sharpe">Maximize Sharpe</SelectItem>
                <SelectItem value="equal_weight">Equal Weight</SelectItem>
                <SelectItem value="manual">Manual</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Capital + Leverage */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="pf-capital">Base Capital ($)</Label>
              <Input
                id="pf-capital"
                type="number"
                value={baseCapital}
                onChange={(e) => setBaseCapital(e.target.value)}
                min={0}
                step={1000}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pf-leverage">Leverage</Label>
              <Input
                id="pf-leverage"
                type="number"
                value={leverage}
                onChange={(e) => setLeverage(e.target.value)}
                min={0}
                step={0.1}
              />
            </div>
          </div>

          {/* Benchmark */}
          <div className="space-y-1.5">
            <Label htmlFor="pf-benchmark">Benchmark Symbol (optional)</Label>
            <Input
              id="pf-benchmark"
              value={benchmark}
              onChange={(e) => setBenchmark(e.target.value)}
              placeholder="SPY"
            />
          </div>

          {/* Allocations */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Allocations</Label>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={addAllocation}
              >
                <Plus className="mr-1 size-3" />
                Add Allocation
              </Button>
            </div>
            {allocations.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No allocations added. You can create the portfolio first and add
                allocations later via the API.
              </p>
            ) : (
              <div className="space-y-2">
                {allocations.map((alloc, idx) => (
                  <div key={idx} className="flex items-end gap-2">
                    <div className="flex-1 space-y-1">
                      {idx === 0 && (
                        <span className="text-xs text-muted-foreground">
                          Candidate ID
                        </span>
                      )}
                      <Input
                        value={alloc.candidate_id}
                        onChange={(e) =>
                          updateAllocation(idx, "candidate_id", e.target.value)
                        }
                        placeholder="UUID"
                        className="font-mono text-xs"
                      />
                    </div>
                    <div className="w-24 space-y-1">
                      {idx === 0 && (
                        <span className="text-xs text-muted-foreground">
                          Weight
                        </span>
                      )}
                      <Input
                        type="number"
                        value={alloc.weight}
                        onChange={(e) =>
                          updateAllocation(idx, "weight", e.target.value)
                        }
                        min={0}
                        max={1}
                        step={0.05}
                      />
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => removeAllocation(idx)}
                      className="shrink-0"
                    >
                      <Trash2 className="size-3.5 text-muted-foreground" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button onClick={() => void handleSubmit()} disabled={submitting}>
            {submitting && <Loader2 className="mr-1.5 size-3.5 animate-spin" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Run Backtest Dialog
// ---------------------------------------------------------------------------

interface RunBacktestDialogProps {
  portfolio: PortfolioResponse;
  onCreated: () => void;
}

function RunBacktestDialog({
  portfolio,
  onCreated,
}: RunBacktestDialogProps): React.ReactElement {
  const { getToken } = useAuth();
  const [open, setOpen] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [startDate, setStartDate] = useState<string>("2024-01-01");
  const [endDate, setEndDate] = useState<string>("2025-01-01");

  const handleSubmit = async (): Promise<void> => {
    if (!startDate || !endDate) {
      setFormError("Start and end dates are required.");
      return;
    }
    if (startDate >= endDate) {
      setFormError("Start date must be before end date.");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    try {
      const token = await getToken();
      await apiPost<PortfolioRunResponse>(
        `/api/v1/portfolios/${portfolio.id}/runs`,
        { start_date: startDate, end_date: endDate },
        token,
      );
      setOpen(false);
      onCreated();
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to launch run (${err.status})`
          : "Failed to launch run";
      setFormError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Play className="mr-1 size-3" />
          Run Backtest
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Run Portfolio Backtest</DialogTitle>
          <DialogDescription>
            Launch a combined backtest for &ldquo;{portfolio.name}&rdquo;
          </DialogDescription>
        </DialogHeader>

        {formError && (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
            {formError}
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label htmlFor={`run-start-${portfolio.id}`}>Start Date</Label>
            <Input
              id={`run-start-${portfolio.id}`}
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`run-end-${portfolio.id}`}>End Date</Label>
            <Input
              id={`run-end-${portfolio.id}`}
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
            />
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => setOpen(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button onClick={() => void handleSubmit()} disabled={submitting}>
            {submitting && <Loader2 className="mr-1.5 size-3.5 animate-spin" />}
            Launch
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Metric snippet helper
// ---------------------------------------------------------------------------

function metricsSnippet(metrics: Record<string, unknown> | null): string {
  if (!metrics) return "--";
  const parts: string[] = [];
  if (typeof metrics.sharpe_ratio === "number")
    parts.push(`S: ${metrics.sharpe_ratio.toFixed(2)}`);
  if (typeof metrics.total_return === "number")
    parts.push(`R: ${(metrics.total_return * 100).toFixed(1)}%`);
  if (typeof metrics.max_drawdown === "number")
    parts.push(`DD: ${(metrics.max_drawdown * 100).toFixed(1)}%`);
  return parts.length > 0 ? parts.join(" | ") : "--";
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function PortfolioPage(): React.ReactElement {
  const { getToken } = useAuth();
  const [portfolios, setPortfolios] = useState<PortfolioResponse[]>([]);
  const [runs, setRuns] = useState<PortfolioRunResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState<boolean>(false);

  // -----------------------------------------------------------------------
  // Data loading
  // -----------------------------------------------------------------------

  const load = useCallback(async (): Promise<void> => {
    try {
      const token = await getToken();
      const [pfData, runsData] = await Promise.all([
        apiGet<PortfolioListResponse>("/api/v1/portfolios", token),
        apiGet<PortfolioRunListResponse>("/api/v1/portfolios/runs", token),
      ]);
      setPortfolios(pfData.items);
      setRuns(runsData.items);
      setError(null);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Failed to load portfolios (${err.status})`
          : "Failed to load portfolios";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    let cancelled = false;
    const doLoad = async (): Promise<void> => {
      await load();
      if (cancelled) return;
    };
    void doLoad();
    return () => {
      cancelled = true;
    };
  }, [load]);

  // -----------------------------------------------------------------------
  // Derived: portfolio name lookup for runs table
  // -----------------------------------------------------------------------

  const portfolioNameById: Record<string, string> = {};
  for (const pf of portfolios) {
    portfolioNameById[pf.id] = pf.name;
  }

  // Sort runs newest-first
  const sortedRuns = [...runs].sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Portfolios</h1>
          <p className="text-sm text-muted-foreground">
            Weighted strategy allocations with combined backtest runs
          </p>
        </div>
        <CreatePortfolioDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          onCreated={() => void load()}
        />
      </div>

      {/* Error banner */}
      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Portfolios table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Portfolios</CardTitle>
          <CardDescription>
            Strategy allocations and portfolio configurations
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading portfolios...
            </div>
          ) : portfolios.length === 0 ? (
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <Briefcase className="size-8 opacity-40" />
              <p>
                No portfolios yet. Click &quot;Create Portfolio&quot; to start.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Name</TableHead>
                  <TableHead>Objective</TableHead>
                  <TableHead className="text-right">Capital</TableHead>
                  <TableHead className="text-right">Leverage</TableHead>
                  <TableHead>Benchmark</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                  <TableHead className="w-32" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {portfolios.map((pf) => (
                  <TableRow key={pf.id} className="border-border/50">
                    <TableCell>
                      <div>
                        <p className="font-medium">{pf.name}</p>
                        {pf.description && (
                          <p className="max-w-xs truncate text-xs text-muted-foreground">
                            {pf.description}
                          </p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={objectiveColor(pf.objective)}
                      >
                        {objectiveLabel(pf.objective)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {formatCurrency(pf.base_capital)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {pf.requested_leverage.toFixed(1)}x
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {pf.benchmark_symbol ?? "--"}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDateTime(pf.created_at)}
                    </TableCell>
                    <TableCell>
                      <RunBacktestDialog
                        portfolio={pf}
                        onCreated={() => void load()}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Recent Runs table */}
      <Card className="border-border/50">
        <CardHeader>
          <CardTitle className="text-base">Recent Runs</CardTitle>
          <CardDescription>
            Combined portfolio backtest results across all portfolios
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
              Loading runs...
            </div>
          ) : sortedRuns.length === 0 ? (
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <PieChart className="size-8 opacity-40" />
              <p>No runs yet. Launch a backtest from a portfolio above.</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border/50 hover:bg-transparent">
                  <TableHead>Portfolio</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Date Range</TableHead>
                  <TableHead>Metrics</TableHead>
                  <TableHead className="text-right">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedRuns.map((run) => (
                  <TableRow key={run.id} className="border-border/50">
                    <TableCell className="font-medium">
                      {portfolioNameById[run.portfolio_id] ??
                        run.portfolio_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="secondary"
                        className={statusColor(run.status)}
                      >
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {run.start_date} &rarr; {run.end_date}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {metricsSnippet(run.metrics)}
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground">
                      {formatDateTime(run.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
