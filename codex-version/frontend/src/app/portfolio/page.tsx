"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type GraduationCandidate = {
  id: string;
  stage: string;
  strategy_name: string;
  instruments: string[];
  selection: {
    metrics?: Record<string, number>;
  };
};

type PortfolioDefinition = {
  id: string;
  name: string;
  description?: string | null;
  objective: "equal_weight" | "maximize_profit" | "maximize_sharpe" | "maximize_sortino" | "manual";
  base_capital: number;
  requested_leverage: number;
  downside_target?: number | null;
  benchmark_symbol?: string | null;
  allocations: Array<{
    candidate_id: string;
    strategy_name: string;
    instruments: string[];
    weight: number;
  }>;
};

type PortfolioRun = {
  id: string;
  portfolio_id: string;
  portfolio_name: string;
  status: string;
  start_date: string;
  end_date: string;
  max_parallelism?: number | null;
  error_message?: string | null;
  metrics?: Record<string, number> | null;
  series: Array<{
    timestamp: string;
    equity: number;
    drawdown: number;
    returns: number;
  }>;
  allocations: Array<{
    candidate_id: string;
    strategy_name: string;
    instruments: string[];
    weight: number;
    metrics?: Record<string, number>;
  }>;
  report_path?: string | null;
};

type AllocationDraft = {
  candidate_id: string;
  weight: string;
};

const PORTFOLIO_OBJECTIVES = [
  { value: "maximize_sharpe", label: "Maximize Sharpe" },
  { value: "maximize_sortino", label: "Maximize Sortino" },
  { value: "maximize_profit", label: "Maximize Profit" },
  { value: "equal_weight", label: "Equal Weight" },
  { value: "manual", label: "Manual" },
] as const;

export default function PortfolioPage() {
  const searchParams = useSearchParams();
  const preselectedCandidateId = searchParams.get("candidate_id");
  const { token } = useAuth();
  const [candidates, setCandidates] = useState<GraduationCandidate[]>([]);
  const [portfolios, setPortfolios] = useState<PortfolioDefinition[]>([]);
  const [runs, setRuns] = useState<PortfolioRun[]>([]);
  const [selectedPortfolioId, setSelectedPortfolioId] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [name, setName] = useState("Core Portfolio");
  const [description, setDescription] = useState("Blended sleeve of graduated strategies");
  const [objective, setObjective] =
    useState<PortfolioDefinition["objective"]>("maximize_sharpe");
  const [baseCapital, setBaseCapital] = useState("1000000");
  const [requestedLeverage, setRequestedLeverage] = useState("1.0");
  const [downsideTarget, setDownsideTarget] = useState("");
  const [benchmarkSymbol, setBenchmarkSymbol] = useState("SPY.EQUS");
  const [runStartDate, setRunStartDate] = useState("2026-03-31");
  const [runEndDate, setRunEndDate] = useState("2026-04-03");
  const [maxParallelism, setMaxParallelism] = useState("2");
  const [allocations, setAllocations] = useState<AllocationDraft[]>([]);
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [queueingRun, setQueueingRun] = useState(false);

  useEffect(() => {
    if (!token) return;

    async function load() {
      try {
        const [candidateRows, portfolioRows, runRows] = await Promise.all([
          apiFetch<GraduationCandidate[]>("/api/v1/graduation/candidates", token),
          apiFetch<PortfolioDefinition[]>("/api/v1/portfolios", token),
          apiFetch<PortfolioRun[]>("/api/v1/portfolios/runs", token),
        ]);
        setCandidates(candidateRows);
        setPortfolios(portfolioRows);
        setRuns(runRows);
        setSelectedPortfolioId((current) => {
          if (current && portfolioRows.some((portfolio) => portfolio.id === current)) return current;
          return portfolioRows[0]?.id ?? "";
        });
        setSelectedRunId((current) => {
          if (current && runRows.some((run) => run.id === current)) return current;
          return runRows[0]?.id ?? "";
        });
        setError("");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load portfolio workspace";
        setError(message);
      }
    }

    void load();
  }, [token]);

  useEffect(() => {
    if (!token) return;
    if (!runs.some((run) => run.status === "pending" || run.status === "running")) return;

    const interval = window.setInterval(() => {
      void apiFetch<PortfolioRun[]>("/api/v1/portfolios/runs", token)
        .then((response) => setRuns(response))
        .catch(() => {
          // Keep the last known state visible; alerts handle hard failures.
        });
    }, 5000);
    return () => window.clearInterval(interval);
  }, [runs, token]);

  useEffect(() => {
    if (!preselectedCandidateId) return;
    if (!candidates.some((candidate) => candidate.id === preselectedCandidateId)) return;
    setAllocations((current) => {
      if (current.some((row) => row.candidate_id === preselectedCandidateId)) {
        return current;
      }
      return [...current, { candidate_id: preselectedCandidateId, weight: "" }];
    });
  }, [candidates, preselectedCandidateId]);

  const selectedPortfolio = useMemo(
    () => portfolios.find((portfolio) => portfolio.id === selectedPortfolioId) ?? null,
    [portfolios, selectedPortfolioId],
  );
  const selectedRun = useMemo(
    () => runs.find((run) => run.id === selectedRunId) ?? null,
    [runs, selectedRunId],
  );
  const candidateLookup = useMemo(
    () => Object.fromEntries(candidates.map((candidate) => [candidate.id, candidate])),
    [candidates],
  );
  const allocationPreview = useMemo(
    () =>
      allocations.map((row) => {
        const candidate = candidateLookup[row.candidate_id];
        return {
          ...row,
          strategy_name: candidate?.strategy_name ?? row.candidate_id,
          instruments: candidate?.instruments.join(", ") ?? "-",
          sharpe: candidate?.selection.metrics?.sharpe,
          total_return: candidate?.selection.metrics?.total_return,
        };
      }),
    [allocations, candidateLookup],
  );

  async function refreshWorkspace() {
    if (!token) return;
    const [portfolioRows, runRows] = await Promise.all([
      apiFetch<PortfolioDefinition[]>("/api/v1/portfolios", token),
      apiFetch<PortfolioRun[]>("/api/v1/portfolios/runs", token),
    ]);
    setPortfolios(portfolioRows);
    setRuns(runRows);
  }

  async function createPortfolio() {
    if (!token) return;
    try {
      setCreating(true);
      const payload = {
        name,
        description,
        objective,
        base_capital: Number(baseCapital),
        requested_leverage: Number(requestedLeverage),
        downside_target: downsideTarget ? Number(downsideTarget) : null,
        benchmark_symbol: benchmarkSymbol || null,
        allocations: allocations.map((row) => ({
          candidate_id: row.candidate_id,
          weight: row.weight ? Number(row.weight) : null,
        })),
      };
      const response = await apiFetch<PortfolioDefinition>("/api/v1/portfolios", token, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await refreshWorkspace();
      setSelectedPortfolioId(response.id);
      setStatusMessage(`Portfolio ${response.name} created.`);
      setError("");
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : "Failed to create portfolio definition";
      setError(message);
    } finally {
      setCreating(false);
    }
  }

  async function queuePortfolioRun(portfolioId: string) {
    if (!token) return;
    try {
      setQueueingRun(true);
      const response = await apiFetch<PortfolioRun>(`/api/v1/portfolios/${portfolioId}/runs`, token, {
        method: "POST",
        body: JSON.stringify({
          start_date: runStartDate,
          end_date: runEndDate,
          max_parallelism: Number(maxParallelism) || 1,
        }),
      });
      await refreshWorkspace();
      setSelectedRunId(response.id);
      setStatusMessage(`Portfolio run ${response.id} queued.`);
      setError("");
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : "Failed to queue portfolio run";
      setError(message);
    } finally {
      setQueueingRun(false);
    }
  }

  function addCandidate(candidateId: string) {
    setAllocations((current) => {
      if (current.some((row) => row.candidate_id === candidateId)) return current;
      return [...current, { candidate_id: candidateId, weight: "" }];
    });
  }

  function updateAllocation(candidateId: string, weight: string) {
    setAllocations((current) =>
      current.map((row) => (row.candidate_id === candidateId ? { ...row, weight } : row)),
    );
  }

  function removeAllocation(candidateId: string) {
    setAllocations((current) => current.filter((row) => row.candidate_id !== candidateId));
  }

  const weightSum = useMemo(
    () =>
      allocations.reduce((sum, row) => {
        const value = Number(row.weight);
        return sum + (Number.isFinite(value) ? value : 0);
      }, 0),
    [allocations],
  );

  return (
    <div className="space-y-6">
      <section className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(167,139,250,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-violet-200/80">Portfolio Lab</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">Model portfolio allocation, downside targets, and research basket behavior before capital goes live.</h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            This workspace builds portfolios from graduated sleeves. It is API-first like the rest of the product: define
            a portfolio, queue a run, inspect the real report, then hand the approved basket into live operations.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <SignalCard label="Candidates available" value={String(candidates.length)} tone="violet" />
            <SignalCard label="Portfolios" value={String(portfolios.length)} tone="cyan" />
            <SignalCard label="Queued / running" value={String(runs.filter((run) => ["pending", "running"].includes(run.status)).length)} tone="amber" />
            <SignalCard label="Completed runs" value={String(runs.filter((run) => run.status === "completed").length)} tone="emerald" />
          </div>
        </div>

        <section className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Current Engine Shape</p>
          <div className="mt-4 space-y-4 text-sm leading-7 text-zinc-300">
            <p>Portfolio definitions and runs are API-backed and queued like research jobs.</p>
            <p>Objectives currently guide sleeve weighting heuristically from candidate metrics.</p>
            <p>Full covariance optimization and multi-strategy-in-one-engine simulation are the next engine-level upgrades.</p>
          </div>
        </section>
      </section>

      {statusMessage ? (
        <div className="rounded-[1.5rem] border border-emerald-300/30 bg-emerald-500/10 p-4 text-sm text-emerald-100">
          {statusMessage}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-[1.5rem] border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <section className="space-y-6 rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div>
            <h3 className="text-lg font-semibold text-white">Create Portfolio Definition</h3>
            <p className="mt-1 text-sm text-zinc-400">
              Select graduated sleeves, set capital and leverage targets, then create a reusable portfolio definition.
            </p>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Name</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Objective</span>
              <select
                value={objective}
                onChange={(event) => setObjective(event.target.value as PortfolioDefinition["objective"])}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              >
                {PORTFOLIO_OBJECTIVES.map((entry) => (
                  <option key={entry.value} value={entry.value}>
                    {entry.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-2 text-sm text-zinc-300 md:col-span-2">
              <span>Description</span>
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={3}
                className="w-full rounded-[1.25rem] border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Base Capital</span>
              <input
                value={baseCapital}
                onChange={(event) => setBaseCapital(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Requested Leverage</span>
              <input
                value={requestedLeverage}
                onChange={(event) => setRequestedLeverage(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Downside Target</span>
              <input
                value={downsideTarget}
                onChange={(event) => setDownsideTarget(event.target.value)}
                placeholder="Optional (e.g. 0.10)"
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Benchmark Symbol</span>
              <input
                value={benchmarkSymbol}
                onChange={(event) => setBenchmarkSymbol(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
          </div>

          <div className="rounded-[1.25rem] border border-white/10 bg-black/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <h4 className="text-sm font-semibold uppercase tracking-[0.22em] text-zinc-300">Graduated sleeves</h4>
              <span className="text-xs text-zinc-500">{allocations.length} selected</span>
            </div>
            <div className="mt-4 grid gap-3">
              {allocations.length === 0 ? (
                <div className="rounded-xl border border-dashed border-white/10 px-4 py-6 text-sm text-zinc-500">
                  Add at least one graduation candidate to define a portfolio.
                </div>
              ) : null}
              {allocationPreview.map((row) => (
                <div key={row.candidate_id} className="grid gap-3 rounded-[1.2rem] border border-white/10 bg-white/[0.03] p-4 md:grid-cols-[1.2fr_1fr_120px_auto] md:items-center">
                  <div>
                    <p className="text-sm font-semibold text-white">{row.strategy_name}</p>
                    <p className="mt-1 text-xs text-zinc-500">{row.instruments}</p>
                  </div>
                  <div className="grid gap-1 text-xs text-zinc-400">
                    <span>Sharpe {formatMetric(row.sharpe)}</span>
                    <span>Return {formatPercent(row.total_return)}</span>
                  </div>
                  <input
                    value={row.weight}
                    onChange={(event) => updateAllocation(row.candidate_id, event.target.value)}
                    placeholder="auto"
                    className="rounded-xl border border-white/10 bg-black/30 px-3 py-3 text-sm text-white"
                  />
                  <button
                    type="button"
                    onClick={() => removeAllocation(row.candidate_id)}
                    className="rounded-xl border border-rose-300/30 px-3 py-3 text-sm text-rose-200"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
            <p className="mt-4 text-xs text-zinc-500">
              Weight input sum: {weightSum.toFixed(2)}. Leave weights blank to let the backend seed them from the chosen objective.
            </p>
          </div>

          <div className="rounded-[1.25rem] border border-white/10 bg-black/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <h4 className="text-sm font-semibold uppercase tracking-[0.22em] text-zinc-300">Available candidates</h4>
              <p className="text-xs text-zinc-500">Only graduation candidates appear here.</p>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              {candidates.length === 0 ? (
                <div className="rounded-xl border border-dashed border-white/10 px-4 py-6 text-sm text-zinc-500 md:col-span-2">
                  No graduation candidates are available yet. Create them from the Research workspace first.
                </div>
              ) : null}
              {candidates.map((candidate) => (
                <button
                  key={candidate.id}
                  type="button"
                  onClick={() => addCandidate(candidate.id)}
                  className="rounded-[1.2rem] border border-white/10 bg-white/[0.03] p-4 text-left transition hover:bg-white/5"
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-semibold text-white">{candidate.strategy_name}</p>
                    <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[10px] uppercase tracking-[0.2em] text-zinc-300">
                      {candidate.stage.replace("_", " ")}
                    </span>
                  </div>
                  <p className="mt-2 text-xs text-zinc-500">{candidate.instruments.join(", ")}</p>
                  <div className="mt-4 grid gap-2 sm:grid-cols-2">
                    <MetricChip label="Sharpe" value={formatMetric(candidate.selection.metrics?.sharpe)} />
                    <MetricChip label="Return" value={formatPercent(candidate.selection.metrics?.total_return)} />
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => void createPortfolio()}
              disabled={creating || allocations.length === 0}
              className="rounded-2xl border border-violet-300/40 bg-violet-500/10 px-4 py-3 text-sm text-violet-100 disabled:opacity-50"
            >
              {creating ? "Creating..." : "Create Portfolio"}
            </button>
          </div>
        </section>

        <section className="space-y-6">
          <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold text-white">Portfolio Definitions</h3>
                <p className="mt-1 text-sm text-zinc-400">Reusable sleeve baskets you can backtest and review.</p>
              </div>
            </div>
            <div className="mt-4 space-y-3">
              {portfolios.length === 0 ? (
                <div className="rounded-xl border border-dashed border-white/10 px-4 py-6 text-sm text-zinc-500">
                  No portfolio definitions yet.
                </div>
              ) : null}
              {portfolios.map((portfolio) => (
                <button
                  key={portfolio.id}
                  type="button"
                  onClick={() => setSelectedPortfolioId(portfolio.id)}
                  className={`w-full rounded-[1.2rem] border p-4 text-left transition ${
                    selectedPortfolioId === portfolio.id
                      ? "border-violet-300/40 bg-violet-500/10"
                      : "border-white/10 bg-white/[0.03] hover:bg-white/5"
                  }`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-base font-semibold text-white">{portfolio.name}</p>
                    <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-zinc-300">
                      {portfolio.objective.replace("_", " ")}
                    </span>
                  </div>
                  <p className="mt-2 text-sm text-zinc-400">{portfolio.description ?? "No description."}</p>
                  <div className="mt-4 grid gap-2 sm:grid-cols-3">
                    <MetricChip label="Capital" value={money(portfolio.base_capital)} />
                    <MetricChip label="Leverage" value={portfolio.requested_leverage.toFixed(2)} />
                    <MetricChip label="Allocations" value={String(portfolio.allocations.length)} />
                  </div>
                </button>
              ))}
            </div>
          </section>

          <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h3 className="text-lg font-semibold text-white">Queue Portfolio Run</h3>
                <p className="mt-1 text-sm text-zinc-400">Run the selected portfolio over a defined time range.</p>
              </div>
              {selectedPortfolio ? (
                <button
                  type="button"
                  onClick={() => void queuePortfolioRun(selectedPortfolio.id)}
                  disabled={queueingRun}
                  className="rounded-2xl border border-emerald-300/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100 disabled:opacity-60"
                >
                  {queueingRun ? "Queueing..." : "Run Portfolio"}
                </button>
              ) : null}
            </div>
            <div className="mt-4 grid gap-4 md:grid-cols-3">
              <label className="space-y-2 text-sm text-zinc-300">
                <span>Start Date</span>
                <input
                  type="date"
                  value={runStartDate}
                  onChange={(event) => setRunStartDate(event.target.value)}
                  className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                />
              </label>
              <label className="space-y-2 text-sm text-zinc-300">
                <span>End Date</span>
                <input
                  type="date"
                  value={runEndDate}
                  onChange={(event) => setRunEndDate(event.target.value)}
                  className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                />
              </label>
              <label className="space-y-2 text-sm text-zinc-300">
                <span>Max Parallelism</span>
                <input
                  value={maxParallelism}
                  onChange={(event) => setMaxParallelism(event.target.value)}
                  className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                />
              </label>
            </div>
          </section>
        </section>
      </div>

      <section className="grid gap-6 xl:grid-cols-[0.9fr_1.1fr]">
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Portfolio Runs</h3>
              <p className="mt-1 text-sm text-zinc-400">Queued and completed portfolio analyses.</p>
            </div>
          </div>
          <div className="mt-4 space-y-3">
            {runs.length === 0 ? (
              <div className="rounded-xl border border-dashed border-white/10 px-4 py-6 text-sm text-zinc-500">
                No portfolio runs yet.
              </div>
            ) : null}
            {runs.map((run) => (
              <button
                key={run.id}
                type="button"
                onClick={() => setSelectedRunId(run.id)}
                className={`w-full rounded-[1.2rem] border p-4 text-left transition ${
                  selectedRunId === run.id
                    ? "border-cyan-300/40 bg-cyan-500/10"
                    : "border-white/10 bg-white/[0.03] hover:bg-white/5"
                }`}
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-base font-semibold text-white">{run.portfolio_name}</p>
                    <p className="mt-1 text-xs text-zinc-500">
                      {run.start_date} → {run.end_date}
                    </p>
                  </div>
                  <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-zinc-300">
                    {run.status}
                  </span>
                </div>
                <div className="mt-4 grid gap-2 sm:grid-cols-3">
                  <MetricChip label="Sharpe" value={formatMetric(run.metrics?.sharpe)} />
                  <MetricChip label="Return" value={formatPercent(run.metrics?.total_return)} />
                  <MetricChip label="Drawdown" value={formatPercent(run.metrics?.max_drawdown)} />
                </div>
                {run.error_message ? <p className="mt-3 text-sm text-rose-200">{run.error_message}</p> : null}
              </button>
            ))}
          </div>
        </section>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Selected Run Detail</h3>
              <p className="mt-1 text-sm text-zinc-400">Review the portfolio series, sleeve weights, and report artifact.</p>
            </div>
            {selectedRun ? (
              <a
                href={`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/v1/portfolios/runs/${selectedRun.id}/report`}
                target="_blank"
                rel="noreferrer"
                className="rounded-2xl border border-cyan-300/30 px-3 py-2 text-sm text-cyan-100"
              >
                Open HTML Report
              </a>
            ) : null}
          </div>

          {selectedRun ? (
            <div className="mt-5 space-y-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <MetricStat label="Sharpe" value={formatMetric(selectedRun.metrics?.sharpe)} />
                <MetricStat label="Sortino" value={formatMetric(selectedRun.metrics?.sortino)} />
                <MetricStat label="Alpha / Beta" value={`${formatMetric(selectedRun.metrics?.alpha)} / ${formatMetric(selectedRun.metrics?.beta)}`} />
                <MetricStat label="Effective Leverage" value={formatMetric(selectedRun.metrics?.effective_leverage)} />
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <section className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                  <h4 className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Equity Curve</h4>
                  <div className="mt-4 h-64">
                    {selectedRun.series.length > 0 ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={selectedRun.series}>
                          <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                          <XAxis dataKey="timestamp" hide />
                          <YAxis stroke="#64748b" tickLine={false} axisLine={false} />
                          <Tooltip
                            labelFormatter={(value) => new Date(value).toLocaleString()}
                            formatter={(value: number) => [money(value), "Equity"]}
                            contentStyle={{
                              backgroundColor: "#09111b",
                              border: "1px solid rgba(255,255,255,0.1)",
                              borderRadius: "16px",
                            }}
                          />
                          <Line type="monotone" dataKey="equity" stroke="#22d3ee" strokeWidth={2.1} dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="This run has not produced a series yet." />
                    )}
                  </div>
                </section>

                <section className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                  <h4 className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Drawdown</h4>
                  <div className="mt-4 h-64">
                    {selectedRun.series.length > 0 ? (
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={selectedRun.series}>
                          <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                          <XAxis dataKey="timestamp" hide />
                          <YAxis stroke="#64748b" tickLine={false} axisLine={false} />
                          <Tooltip
                            labelFormatter={(value) => new Date(value).toLocaleString()}
                            formatter={(value: number) => [formatPercent(value), "Drawdown"]}
                            contentStyle={{
                              backgroundColor: "#09111b",
                              border: "1px solid rgba(255,255,255,0.1)",
                              borderRadius: "16px",
                            }}
                          />
                          <Area type="monotone" dataKey="drawdown" stroke="#f43f5e" fill="#f43f5e33" />
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="Drawdown series will appear once the run completes." />
                    )}
                  </div>
                </section>
              </div>

              <div className="overflow-x-auto">
                <table className="min-w-full text-sm text-zinc-200">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
                      <th className="pb-3">Sleeve</th>
                      <th className="pb-3">Instruments</th>
                      <th className="pb-3">Weight</th>
                      <th className="pb-3">Sharpe</th>
                      <th className="pb-3">Return</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedRun.allocations.map((allocation) => (
                      <tr key={allocation.candidate_id} className="border-t border-white/10">
                        <td className="py-3 font-medium text-white">{allocation.strategy_name}</td>
                        <td className="py-3 text-zinc-400">{allocation.instruments.join(", ")}</td>
                        <td className="py-3">{formatPercent(allocation.weight)}</td>
                        <td className="py-3">{formatMetric(allocation.metrics?.sharpe)}</td>
                        <td className="py-3">{formatPercent(allocation.metrics?.total_return)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="mt-4 rounded-[1.25rem] border border-dashed border-white/10 bg-black/20 px-4 py-10 text-sm text-zinc-500">
              Select a portfolio run to inspect the resulting analytics and tearsheet.
            </div>
          )}
        </section>
      </section>

      {selectedPortfolio ? (
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Selected Portfolio Definition</h3>
              <p className="mt-1 text-sm text-zinc-400">Current reusable allocation model and operating links.</p>
            </div>
            <Link href="/graduation" className="rounded-2xl border border-white/10 px-3 py-2 text-sm text-zinc-200">
              Review Graduation Queue
            </Link>
          </div>
          <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricStat label="Objective" value={selectedPortfolio.objective.replace("_", " ")} />
            <MetricStat label="Benchmark" value={selectedPortfolio.benchmark_symbol ?? "N/A"} />
            <MetricStat label="Capital" value={money(selectedPortfolio.base_capital)} />
            <MetricStat label="Leverage" value={selectedPortfolio.requested_leverage.toFixed(2)} />
          </div>
        </section>
      ) : null}
    </div>
  );
}

function SignalCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "violet" | "cyan" | "amber" | "emerald";
}) {
  const toneClasses =
    tone === "violet"
      ? "border-violet-300/20 bg-violet-400/10 text-violet-50"
      : tone === "cyan"
        ? "border-cyan-300/20 bg-cyan-400/10 text-cyan-50"
        : tone === "amber"
          ? "border-amber-300/20 bg-amber-400/10 text-amber-50"
          : "border-emerald-300/20 bg-emerald-400/10 text-emerald-50";
  return (
    <div className={`rounded-2xl border px-4 py-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}

function MetricChip({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-black/20 px-3 py-2">
      <p className="text-[10px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className="mt-2 text-sm text-zinc-100">{value}</p>
    </div>
  );
}

function MetricStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className="mt-3 text-sm font-semibold text-white">{value}</p>
    </div>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center rounded-[1.2rem] border border-dashed border-white/10 bg-black/20 px-6 text-center text-sm text-zinc-500">
      {message}
    </div>
  );
}

function money(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatMetric(value: number | undefined): string {
  return value === undefined ? "N/A" : value.toFixed(2);
}

function formatPercent(value: number | undefined): string {
  return value === undefined ? "N/A" : `${(value * 100).toFixed(2)}%`;
}
