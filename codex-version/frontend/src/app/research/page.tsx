"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type ResearchSummary = {
  id: string;
  mode: string;
  generated_at?: string | null;
  strategy_name?: string | null;
  instruments: string[];
  objective?: string | null;
  summary: Record<string, number | string | null>;
  best_config?: Record<string, unknown> | null;
  best_metrics?: Record<string, number> | null;
  candidate_count: number;
};

type SweepResult = {
  config: Record<string, unknown>;
  metrics?: Record<string, number>;
  error?: string | null;
  start_date?: string;
  end_date?: string;
};

type WalkForwardWindow = {
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  best_train_result?: SweepResult | null;
  test_result?: SweepResult | null;
};

type ResearchDetail = {
  summary: ResearchSummary;
  report: {
    mode: string;
    generated_at?: string;
    start_date?: string;
    end_date?: string;
    objective?: string;
    instruments: string[];
    results?: SweepResult[];
    windows?: WalkForwardWindow[];
  };
};

type PromotionDraft = {
  id: string;
  report_id: string;
  strategy_id: string;
  strategy_name: string;
  config: Record<string, unknown>;
  instruments: string[];
  live_url: string;
  created_at: string;
  selection: {
    kind: string;
    result_index?: number | null;
    window_index?: number | null;
    metrics?: Record<string, number>;
  };
};

type GraduationCandidate = {
  id: string;
  strategy_id: string;
  strategy_name: string;
  stage: string;
  instruments: string[];
  live_url: string;
  portfolio_url: string;
};

type ResearchJobSummary = {
  id: string;
  job_type: "parameter_sweep" | "walk_forward";
  status: string;
  progress: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
  report_id?: string | null;
  strategy_id: string;
  strategy_name: string;
  strategy_path: string;
  instruments: string[];
  objective?: string | null;
};

type StrategySummary = {
  id: string;
  name: string;
  description?: string | null;
};

type StrategyDetail = {
  id: string;
  name: string;
  default_config?: Record<string, unknown> | null;
};

type PortfolioAllocationRow = {
  id: string;
  name: string;
  instruments: string;
  sharpe: number | null;
  totalReturn: number | null;
  weight: number;
  capital: number;
};

export default function ResearchPage() {
  const { token } = useAuth();
  const [reports, setReports] = useState<ResearchSummary[]>([]);
  const [jobs, setJobs] = useState<ResearchJobSummary[]>([]);
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [runMode, setRunMode] = useState<"parameter_sweep" | "walk_forward">("parameter_sweep");
  const [instrumentsInput, setInstrumentsInput] = useState("SPY.EQUS");
  const [startDate, setStartDate] = useState("2026-03-31");
  const [endDate, setEndDate] = useState("2026-04-03");
  const [objective, setObjective] = useState("sharpe");
  const [maxParallelism, setMaxParallelism] = useState("2");
  const [trainDays, setTrainDays] = useState("2");
  const [testDays, setTestDays] = useState("1");
  const [stepDays, setStepDays] = useState("1");
  const [baseConfigText, setBaseConfigText] = useState("{\n  \"lookback\": 20,\n  \"zscore_threshold\": 1.5\n}");
  const [parameterGridText, setParameterGridText] = useState("{\n  \"lookback\": [10, 20, 30],\n  \"zscore_threshold\": [1.0, 1.5, 2.0]\n}");
  const [jobMessage, setJobMessage] = useState("");
  const [submittingJob, setSubmittingJob] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [activeReportId, setActiveReportId] = useState("");
  const [detail, setDetail] = useState<ResearchDetail | null>(null);
  const [comparison, setComparison] = useState<ResearchDetail[]>([]);
  const [promotion, setPromotion] = useState<PromotionDraft | null>(null);
  const [candidate, setCandidate] = useState<GraduationCandidate | null>(null);
  const [error, setError] = useState("");
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [portfolioCapital, setPortfolioCapital] = useState("1000000");
  const [portfolioLeverage, setPortfolioLeverage] = useState("1.0");

  useEffect(() => {
    if (!token) return;

    async function loadReports() {
      try {
        const response = await apiFetch<ResearchSummary[]>("/api/v1/research/reports", token);
        setReports(response);
        if (response[0] && !activeReportId) {
          setActiveReportId(response[0].id);
        }
        setSelectedIds((current) => current.filter((id) => response.some((report) => report.id === id)));
        setError("");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load research reports";
        setError(message);
      }
    }

    void loadReports();
  }, [activeReportId, token]);

  useEffect(() => {
    if (!token) return;

    async function loadStrategies() {
      try {
        const response = await apiFetch<StrategySummary[]>("/api/v1/strategies/", token);
        setStrategies(response);
        if (!selectedStrategyId && response[0]) {
          setSelectedStrategyId(response[0].id);
        }
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load strategies";
        setError(message);
      }
    }

    void loadStrategies();
  }, [selectedStrategyId, token]);

  useEffect(() => {
    if (!token) return;

    async function loadJobs() {
      try {
        const response = await apiFetch<ResearchJobSummary[]>("/api/v1/research/jobs", token);
        setJobs(response);
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load research jobs";
        setError(message);
      }
    }

    void loadJobs();
    const interval = window.setInterval(() => void loadJobs(), 5000);
    return () => window.clearInterval(interval);
  }, [token]);

  useEffect(() => {
    if (!token || !selectedStrategyId) return;

    async function loadStrategyDefaults() {
      try {
        const strategy = await apiFetch<StrategyDetail>(`/api/v1/strategies/${selectedStrategyId}`, token);
        const preset = strategyPreset(strategy.name);
        const baseConfig =
          strategy.default_config && Object.keys(strategy.default_config).length > 0
            ? strategy.default_config
            : preset.baseConfig;
        setBaseConfigText(JSON.stringify(baseConfig, null, 2));
        setParameterGridText(JSON.stringify(preset.parameterGrid, null, 2));
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load strategy defaults";
        setError(message);
      }
    }

    void loadStrategyDefaults();
  }, [selectedStrategyId, token]);

  useEffect(() => {
    if (!token || !jobs.some((job) => job.status === "completed" && job.report_id)) {
      return;
    }

    void (async () => {
      try {
        const response = await apiFetch<ResearchSummary[]>("/api/v1/research/reports", token);
        setReports(response);
        if (response[0] && !activeReportId) {
          setActiveReportId(response[0].id);
        }
      } catch {
        // Keep current reports if refresh fails; the jobs list still provides visibility.
      }
    })();
  }, [activeReportId, jobs, token]);

  useEffect(() => {
    if (!token || !activeReportId) return;

    async function loadDetail() {
      try {
        setLoadingDetail(true);
        const response = await apiFetch<ResearchDetail>(`/api/v1/research/reports/${activeReportId}`, token);
        setDetail(response);
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load report detail";
        setError(message);
      } finally {
        setLoadingDetail(false);
      }
    }

    void loadDetail();
  }, [activeReportId, token]);

  useEffect(() => {
    if (!token || selectedIds.length < 2) {
      setComparison([]);
      return;
    }

    async function loadComparison() {
      try {
        const response = await apiFetch<{ reports: ResearchDetail[] }>("/api/v1/research/compare", token, {
          method: "POST",
          body: JSON.stringify({ report_ids: selectedIds.slice(0, 3) }),
        });
        setComparison(response.reports);
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to compare research reports";
        setError(message);
      }
    }

    void loadComparison();
  }, [selectedIds, token]);

  const leaderboard = useMemo(() => detail?.report.results ?? [], [detail]);
  const windows = useMemo(() => detail?.report.windows ?? [], [detail]);
  const selectedReports = useMemo(
    () => reports.filter((report) => selectedIds.includes(report.id)),
    [reports, selectedIds],
  );
  const reportCount = reports.length;
  const pendingJobs = jobs.filter((job) => !["completed", "failed"].includes(job.status)).length;
  const successfulJobs = jobs.filter((job) => job.status === "completed").length;
  const portfolioPreview = useMemo(
    () => buildPortfolioPreview(selectedReports, Number(portfolioCapital) || 0, Number(portfolioLeverage) || 0),
    [portfolioCapital, portfolioLeverage, selectedReports],
  );
  const leaderboardChart = useMemo(
    () =>
      leaderboard.slice(0, 6).map((result, index) => ({
        name: `#${index + 1}`,
        sharpe: Number(result.metrics?.sharpe ?? 0),
        return: Number(result.metrics?.total_return ?? 0),
      })),
    [leaderboard],
  );
  const comparisonChart = useMemo(
    () =>
      comparison.map((report) => ({
        name: (report.summary.strategy_name ?? report.summary.id).replace("example.", ""),
        sharpe: Number(report.summary.best_metrics?.sharpe ?? 0),
        drawdown: Number(report.summary.best_metrics?.max_drawdown ?? 0),
      })),
    [comparison],
  );

  async function createPromotion(resultIndex?: number, windowIndex?: number) {
    if (!token || !detail) return;
    try {
      const draft = await apiFetch<PromotionDraft>("/api/v1/research/promotions", token, {
        method: "POST",
        body: JSON.stringify({
          report_id: detail.summary.id,
          result_index: resultIndex ?? null,
          window_index: windowIndex ?? null,
          paper_trading: true,
        }),
      });
      setPromotion(draft);
      const graduationCandidate = await apiFetch<GraduationCandidate>("/api/v1/graduation/candidates", token, {
        method: "POST",
        body: JSON.stringify({
          promotion_id: draft.id,
        }),
      });
      setCandidate(graduationCandidate);
      setError("");
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : "Failed to create paper promotion";
      setError(message);
    }
  }

  async function submitResearchJob() {
    if (!token || !selectedStrategyId) return;

    try {
      setSubmittingJob(true);
      const baseConfig = JSON.parse(baseConfigText) as Record<string, unknown>;
      const parameterGrid = JSON.parse(parameterGridText) as Record<string, unknown[]>;
      const payload: Record<string, unknown> = {
        strategy_id: selectedStrategyId,
        instruments: instrumentsInput
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
        start_date: startDate,
        end_date: endDate,
        base_config: baseConfig,
        parameter_grid: parameterGrid,
        objective,
        max_parallelism: Number(maxParallelism) || 1,
      };
      if (runMode === "walk_forward") {
        payload.train_days = Number(trainDays);
        payload.test_days = Number(testDays);
        payload.step_days = Number(stepDays);
        payload.mode = "rolling";
      }
      const endpoint = runMode === "walk_forward" ? "/api/v1/research/walk-forward" : "/api/v1/research/sweeps";
      const response = await apiFetch<{ job_id: string; status: string }>(endpoint, token, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setJobMessage(`Queued ${runMode.replace("_", " ")} job ${response.job_id}`);
      const refreshedJobs = await apiFetch<ResearchJobSummary[]>("/api/v1/research/jobs", token);
      setJobs(refreshedJobs);
      setError("");
    } catch (submitError) {
      const message = submitError instanceof Error ? submitError.message : "Failed to queue research job";
      setError(message);
    } finally {
      setSubmittingJob(false);
    }
  }

  function toggleSelection(reportId: string) {
    setSelectedIds((current) => {
      if (current.includes(reportId)) {
        return current.filter((id) => id !== reportId);
      }
      if (current.length >= 20) {
        return current;
      }
      return [...current, reportId];
    });
  }

  return (
    <div className="space-y-6">
      <section className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(45,212,191,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-cyan-200/80">Phase 3</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">Research Console</h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            This is the workspace where strategy code graduates from idea to candidate portfolio. Submit sweeps through
            the API, validate generalization with walk-forward windows, compare the winners, and create promotion drafts
            without leaving the same control surface.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <SignalCard label="Saved reports" value={String(reportCount)} tone="cyan" />
            <SignalCard label="Queued / running" value={String(pendingJobs)} tone="amber" />
            <SignalCard label="Completed jobs" value={String(successfulJobs)} tone="emerald" />
            <SignalCard label="Portfolio candidates" value={String(selectedReports.length)} tone="violet" />
          </div>
        </div>

        <section className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Portfolio Construction Lab</p>
              <h3 className="mt-2 text-xl font-semibold text-white">Selected strategy basket</h3>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              Equal-weight preview
            </span>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Capital</span>
              <input
                value={portfolioCapital}
                onChange={(event) => setPortfolioCapital(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Leverage</span>
              <input
                value={portfolioLeverage}
                onChange={(event) => setPortfolioLeverage(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <MetricPanel label="Gross capital deployed" value={money(portfolioPreview.grossCapital)} />
            <MetricPanel label="Per strategy" value={money(portfolioPreview.capitalPerStrategy)} />
            <MetricPanel label="Blended Sharpe" value={formatMetric(portfolioPreview.averageSharpe)} />
            <MetricPanel label="Blended Return" value={formatPercent(portfolioPreview.averageReturn)} />
            <MetricPanel label="Win rate" value={formatPercent(portfolioPreview.averageWinRate)} />
            <MetricPanel label="Alpha / Beta" value={`${formatMetricOrPending(portfolioPreview.averageAlpha)} / ${formatMetricOrPending(portfolioPreview.averageBeta)}`} />
          </div>
          <p className="mt-4 text-xs leading-6 text-zinc-500">
            This preview is intentionally honest: today it blends report-level metrics for quick allocation shaping. Full
            covariance, benchmark-relative alpha/beta, and exact margin analytics still need dedicated backend portfolio
            construction support. Once you graduate candidates, move into the Portfolio workspace for a real basket
            definition and queued portfolio runs.
          </p>
        </section>
      </section>

      <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold text-white">Launch Research Jobs</h3>
            <p className="mt-1 max-w-3xl text-sm leading-6 text-zinc-400">
              Submit parameter sweeps and walk-forward runs through the same API the worker consumes. The API returns
              immediately, and the research worker executes the heavy Nautilus backtests in the background with process-level parallelism.
            </p>
          </div>
          {jobMessage ? <p className="text-sm text-emerald-200">{jobMessage}</p> : null}
        </div>

        <div className="mt-5 grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Strategy</span>
              <select
                value={selectedStrategyId}
                onChange={(event) => setSelectedStrategyId(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              >
                {strategies.map((strategy) => (
                  <option key={strategy.id} value={strategy.id}>
                    {strategy.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Mode</span>
              <select
                value={runMode}
                onChange={(event) => setRunMode(event.target.value as "parameter_sweep" | "walk_forward")}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              >
                <option value="parameter_sweep">Parameter Sweep</option>
                <option value="walk_forward">Walk Forward</option>
              </select>
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Instruments</span>
              <input
                value={instrumentsInput}
                onChange={(event) => setInstrumentsInput(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Objective</span>
              <input
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Start Date</span>
              <input
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>End Date</span>
              <input
                type="date"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Max Parallelism</span>
              <input
                type="number"
                min={1}
                max={32}
                value={maxParallelism}
                onChange={(event) => setMaxParallelism(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              />
            </label>
            {runMode === "walk_forward" ? (
              <div className="grid gap-4 md:grid-cols-3 md:col-span-2">
                <label className="space-y-2 text-sm text-zinc-300">
                  <span>Train Days</span>
                  <input
                    type="number"
                    min={1}
                    value={trainDays}
                    onChange={(event) => setTrainDays(event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                  />
                </label>
                <label className="space-y-2 text-sm text-zinc-300">
                  <span>Test Days</span>
                  <input
                    type="number"
                    min={1}
                    value={testDays}
                    onChange={(event) => setTestDays(event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                  />
                </label>
                <label className="space-y-2 text-sm text-zinc-300">
                  <span>Step Days</span>
                  <input
                    type="number"
                    min={1}
                    value={stepDays}
                    onChange={(event) => setStepDays(event.target.value)}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                  />
                </label>
              </div>
            ) : null}
          </div>

          <div className="grid gap-4">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Base Config JSON</span>
              <textarea
                value={baseConfigText}
                onChange={(event) => setBaseConfigText(event.target.value)}
                rows={10}
                className="w-full rounded-[1.25rem] border border-white/10 bg-black/30 px-3 py-3 font-mono text-xs text-white"
              />
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Parameter Grid JSON</span>
              <textarea
                value={parameterGridText}
                onChange={(event) => setParameterGridText(event.target.value)}
                rows={10}
                className="w-full rounded-[1.25rem] border border-white/10 bg-black/30 px-3 py-3 font-mono text-xs text-white"
              />
            </label>
          </div>
        </div>

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-zinc-500">
            Queue jobs from the UI, the terminal, or an agent. The surface is the same because the product is API-first.
          </p>
          <button
            type="button"
            onClick={() => void submitResearchJob()}
            disabled={submittingJob || !selectedStrategyId}
            className="rounded-2xl border border-cyan-300/40 bg-cyan-500/10 px-4 py-3 text-sm text-cyan-100 disabled:opacity-50"
          >
            {submittingJob ? "Queueing..." : "Queue Research Job"}
          </button>
        </div>
      </section>

      <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-lg font-semibold text-white">Research Job Queue</h3>
          <p className="text-xs text-zinc-400">Background runs submitted via API</p>
        </div>
        <div className="mt-4 grid gap-3 xl:grid-cols-3">
          {jobs.length === 0 ? <p className="text-sm text-zinc-400">No queued research jobs yet.</p> : null}
          {jobs.map((job) => (
            <div key={job.id} className="rounded-[1.2rem] border border-white/10 bg-white/[0.03] p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{job.job_type.replace("_", " ")}</p>
                  <p className="mt-2 text-base font-semibold text-white">{job.strategy_name}</p>
                  <p className="mt-1 text-xs text-zinc-500">{job.instruments.join(", ")}</p>
                </div>
                <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-zinc-300">
                  {job.status}
                </span>
              </div>
              <div className="mt-4 h-2 rounded-full bg-white/5">
                <div className="h-2 rounded-full bg-cyan-300/70" style={{ width: `${job.progress}%` }} />
              </div>
              <p className="mt-2 text-xs text-zinc-500">{job.progress}% complete</p>
              {job.error_message ? <p className="mt-3 text-sm text-rose-200">{job.error_message}</p> : null}
              {job.report_id ? (
                <button
                  type="button"
                  onClick={() => setActiveReportId(job.report_id ?? "")}
                  className="mt-4 rounded-2xl border border-emerald-300/30 px-3 py-2 text-xs text-emerald-100"
                >
                  Open Saved Report
                </button>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {promotion ? (
        <div className="rounded-[1.5rem] border border-emerald-300/30 bg-emerald-500/10 p-5 text-sm text-emerald-100">
          <p className="font-semibold">Graduation candidate created</p>
          <p className="mt-2">
            {promotion.strategy_name} is ready for graduation review with instruments {promotion.instruments.join(", ")}.
          </p>
          <div className="mt-4 flex flex-wrap gap-3">
            {candidate ? (
              <Link href={`/graduation?candidate_id=${candidate.id}`} className="inline-flex rounded-2xl border border-emerald-300/40 px-4 py-2">
                Open in Graduation
              </Link>
            ) : null}
            {candidate ? (
              <Link href={candidate.portfolio_url} className="inline-flex rounded-2xl border border-violet-300/40 px-4 py-2 text-violet-100">
                Open in Portfolio
              </Link>
            ) : null}
            <Link href={promotion.live_url} className="inline-flex rounded-2xl border border-cyan-300/40 px-4 py-2 text-cyan-100">
              Open in Live Trading
            </Link>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="rounded-[1.5rem] border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[0.95fr_1.25fr]">
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Saved Reports</h3>
              <p className="mt-1 text-sm text-zinc-400">Select up to 20 for portfolio preview. The first 3 also drive side-by-side comparison.</p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              {selectedReports.length} selected
            </span>
          </div>
          <div className="mt-4 space-y-3">
            {reports.length === 0 ? (
              <p className="text-sm text-zinc-400">No research reports found yet. Run a sweep or walk-forward job first.</p>
            ) : null}
            {reports.map((report) => {
              const selected = selectedIds.includes(report.id);
              const selectionDisabled = !selected && selectedIds.length >= 20;
              return (
                <div
                  key={report.id}
                  className={`rounded-[1.2rem] border p-4 transition ${
                    activeReportId === report.id
                      ? "border-cyan-300/40 bg-cyan-500/10"
                      : "border-white/10 bg-white/[0.03] hover:bg-white/5"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{report.mode.replace("_", " ")}</p>
                      <p className="mt-2 text-base font-semibold text-white">{report.strategy_name ?? report.id}</p>
                      <p className="mt-1 text-xs text-zinc-500">{report.instruments.join(", ")}</p>
                    </div>
                    <div className="flex items-center gap-3">
                      <label className="flex items-center gap-2 text-xs text-zinc-300">
                        <input
                          type="checkbox"
                          checked={selected}
                          disabled={selectionDisabled}
                          onChange={() => toggleSelection(report.id)}
                        />
                        Basket
                      </label>
                      <button
                        type="button"
                        onClick={() => setActiveReportId(report.id)}
                        className="rounded-2xl border border-white/10 px-3 py-2 text-xs text-zinc-200"
                      >
                        Inspect
                      </button>
                    </div>
                  </div>
                  <div className="mt-4 grid gap-2 sm:grid-cols-3">
                    <MetricPanel label="Candidates" value={String(report.candidate_count)} compact />
                    <MetricPanel label="Sharpe" value={formatMetric(report.best_metrics?.sharpe)} compact />
                    <MetricPanel label="Return" value={formatPercent(report.best_metrics?.total_return)} compact />
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Report Detail</h3>
              {detail ? (
                <p className="mt-1 text-sm text-zinc-400">
                  {detail.summary.strategy_name} · {detail.summary.instruments.join(", ")}
                </p>
              ) : null}
            </div>
            {detail && detail.report.mode === "parameter_sweep" ? (
              <button
                type="button"
                onClick={() => void createPromotion(0)}
                className="rounded-2xl border border-emerald-300/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-100"
              >
                Promote Top Config
              </button>
            ) : null}
          </div>

          {loadingDetail ? <p className="mt-4 text-sm text-zinc-400">Loading report detail...</p> : null}

          {detail ? (
            <div className="mt-5 space-y-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <MetricPanel label="Mode" value={detail.summary.mode.replace("_", " ")} />
                <MetricPanel label="Objective" value={detail.summary.objective ?? "-"} />
                <MetricPanel label="Best Sharpe" value={formatMetric(detail.summary.best_metrics?.sharpe)} />
                <MetricPanel label="Best Return" value={formatPercent(detail.summary.best_metrics?.total_return)} />
              </div>

              {leaderboardChart.length > 0 ? (
                <section className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Parameter Leaderboard</h4>
                    <p className="text-xs text-zinc-500">Top-ranked configs by current objective</p>
                  </div>
                  <div className="mt-4 h-60">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={leaderboardChart} margin={{ top: 8, right: 12, left: -20, bottom: 0 }}>
                        <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                        <XAxis dataKey="name" stroke="#64748b" tickLine={false} axisLine={false} />
                        <YAxis stroke="#64748b" tickLine={false} axisLine={false} />
                        <Tooltip
                          contentStyle={{
                            backgroundColor: "#09111b",
                            border: "1px solid rgba(255,255,255,0.1)",
                            borderRadius: "16px",
                          }}
                        />
                        <Bar dataKey="sharpe" radius={[8, 8, 0, 0]} fill="#22d3ee" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </section>
              ) : null}

              {leaderboard.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="min-w-full text-sm text-zinc-200">
                    <thead className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
                      <tr>
                        <th className="pb-3">#</th>
                        <th className="pb-3">Config</th>
                        <th className="pb-3">Sharpe</th>
                        <th className="pb-3">Return</th>
                        <th className="pb-3">Win Rate</th>
                        <th className="pb-3 text-right">Promote</th>
                      </tr>
                    </thead>
                    <tbody>
                      {leaderboard.slice(0, 10).map((result, index) => (
                        <tr key={`${detail.summary.id}-result-${index}`} className="border-t border-white/5">
                          <td className="py-3 text-zinc-400">{index + 1}</td>
                          <td className="py-3 font-mono text-xs text-zinc-300">{formatConfig(result.config)}</td>
                          <td className="py-3">{formatMetric(result.metrics?.sharpe)}</td>
                          <td className="py-3">{formatPercent(result.metrics?.total_return)}</td>
                          <td className="py-3">{formatPercent(result.metrics?.win_rate)}</td>
                          <td className="py-3 text-right">
                            <button
                              type="button"
                              onClick={() => void createPromotion(index)}
                              className="rounded-2xl border border-cyan-300/30 px-3 py-2 text-xs text-cyan-100"
                            >
                              Graduate
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}

              {windows.length > 0 ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Walk-Forward Windows</h4>
                    <p className="text-xs text-zinc-500">Review train/test behavior before paper promotion</p>
                  </div>
                  <div className="grid gap-3">
                    {windows.map((window, index) => (
                      <div key={`${detail.summary.id}-window-${index}`} className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p className="text-sm font-semibold text-white">Window {index + 1}</p>
                            <p className="mt-1 text-xs text-zinc-500">
                              Train {window.train_start} → {window.train_end} · Test {window.test_start} → {window.test_end}
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={() => void createPromotion(undefined, index)}
                            className="rounded-2xl border border-cyan-300/30 px-3 py-2 text-xs text-cyan-100"
                          >
                            Graduate
                          </button>
                        </div>
                        <div className="mt-4 grid gap-3 sm:grid-cols-3">
                          <MetricPanel label="Best Config" value={formatConfig(window.best_train_result?.config ?? {})} compact />
                          <MetricPanel label="Test Sharpe" value={formatMetric(window.test_result?.metrics?.sharpe)} compact />
                          <MetricPanel label="Test Return" value={formatPercent(window.test_result?.metrics?.total_return)} compact />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="mt-4 text-sm text-zinc-400">Select a research report to inspect it.</p>
          )}
        </section>
      </div>

      {selectedReports.length > 0 ? (
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Portfolio Preview</h3>
              <p className="mt-1 text-sm text-zinc-400">Equal-weight allocation sketch for the reports you selected.</p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              {selectedReports.length} strategies
            </span>
          </div>
          <div className="mt-4 grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
                    <th className="pb-3">Strategy</th>
                    <th className="pb-3">Universe</th>
                    <th className="pb-3">Weight</th>
                    <th className="pb-3">Capital</th>
                    <th className="pb-3">Sharpe</th>
                    <th className="pb-3">Return</th>
                  </tr>
                </thead>
                <tbody>
                  {portfolioPreview.allocations.map((row) => (
                    <tr key={row.id} className="border-t border-white/10 text-zinc-200">
                      <td className="py-3 font-medium text-white">{row.name}</td>
                      <td className="py-3 text-zinc-400">{row.instruments}</td>
                      <td className="py-3">{formatPercent(row.weight)}</td>
                      <td className="py-3">{money(row.capital)}</td>
                      <td className="py-3">{formatMetric(row.sharpe)}</td>
                      <td className="py-3">{formatPercent(row.totalReturn)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Allocation profile</p>
                <p className="text-xs text-zinc-500">{portfolioPreview.marginMode}</p>
              </div>
              <div className="mt-4 h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={portfolioPreview.allocations} margin={{ top: 8, right: 12, left: -20, bottom: 0 }}>
                    <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                    <XAxis dataKey="name" stroke="#64748b" tickLine={false} axisLine={false} />
                    <YAxis stroke="#64748b" tickLine={false} axisLine={false} tickFormatter={(value) => `$${Math.round(value / 1000)}k`} />
                    <Tooltip
                      formatter={(value: number) => [money(value), "Capital"]}
                      contentStyle={{
                        backgroundColor: "#09111b",
                        border: "1px solid rgba(255,255,255,0.1)",
                        borderRadius: "16px",
                      }}
                    />
                    <Bar dataKey="capital" radius={[8, 8, 0, 0]}>
                      {portfolioPreview.allocations.map((row, index) => (
                        <Cell key={row.id} fill={PORTFOLIO_COLORS[index % PORTFOLIO_COLORS.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </section>
      ) : null}

      {comparison.length >= 2 ? (
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-lg font-semibold text-white">Side-by-Side Comparison</h3>
            <p className="text-xs text-zinc-400">Comparing {comparison.length} saved reports</p>
          </div>
          <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_1fr]">
            <div className="grid gap-4 lg:grid-cols-3">
              {comparison.map((report) => (
                <div key={report.summary.id} className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                  <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{report.summary.mode.replace("_", " ")}</p>
                  <p className="mt-2 text-lg font-semibold text-white">{report.summary.strategy_name ?? report.summary.id}</p>
                  <p className="mt-1 text-xs text-zinc-500">{report.summary.instruments.join(", ")}</p>
                  <div className="mt-4 space-y-2 text-sm text-zinc-200">
                    <ComparisonRow label="Best Sharpe" value={formatMetric(report.summary.best_metrics?.sharpe)} />
                    <ComparisonRow label="Best Return" value={formatPercent(report.summary.best_metrics?.total_return)} />
                    <ComparisonRow label="Best Win Rate" value={formatPercent(report.summary.best_metrics?.win_rate)} />
                    <ComparisonRow label="Candidates" value={String(report.summary.candidate_count)} />
                  </div>
                </div>
              ))}
            </div>

            <div className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
              <p className="text-sm font-semibold uppercase tracking-[0.2em] text-zinc-300">Sharpe comparison</p>
              <div className="mt-4 h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={comparisonChart} margin={{ top: 8, right: 12, left: -20, bottom: 0 }}>
                    <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                    <XAxis dataKey="name" stroke="#64748b" tickLine={false} axisLine={false} />
                    <YAxis stroke="#64748b" tickLine={false} axisLine={false} />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "#09111b",
                        border: "1px solid rgba(255,255,255,0.1)",
                        borderRadius: "16px",
                      }}
                    />
                    <Bar dataKey="sharpe" radius={[8, 8, 0, 0]} fill="#22d3ee" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </section>
      ) : null}
    </div>
  );
}

const PORTFOLIO_COLORS = ["#22d3ee", "#2dd4bf", "#818cf8", "#f59e0b", "#fb7185", "#38bdf8"];

function strategyPreset(strategyName: string | null | undefined): {
  baseConfig: Record<string, unknown>;
  parameterGrid: Record<string, unknown[]>;
} {
  switch (strategyName) {
    case "example.donchian_breakout":
      return {
        baseConfig: { channel_period: 20, trailing_stop_atr_multiple: 2.0 },
        parameterGrid: {
          channel_period: [10, 20, 30],
          trailing_stop_atr_multiple: [1.5, 2.0, 2.5],
        },
      };
    case "example.ema_cross":
      return {
        baseConfig: { fast_period: 10, slow_period: 30 },
        parameterGrid: {
          fast_period: [5, 10, 15],
          slow_period: [20, 30, 40],
        },
      };
    default:
      return {
        baseConfig: { lookback: 20, zscore_threshold: 1.5 },
        parameterGrid: {
          lookback: [10, 20, 30],
          zscore_threshold: [1.0, 1.5, 2.0],
        },
      };
  }
}

function buildPortfolioPreview(
  reports: ResearchSummary[],
  capital: number,
  leverage: number,
): {
  allocations: PortfolioAllocationRow[];
  grossCapital: number;
  capitalPerStrategy: number;
  averageSharpe: number | null;
  averageReturn: number | null;
  averageWinRate: number | null;
  averageAlpha: number | null;
  averageBeta: number | null;
  marginMode: string;
} {
  const grossCapital = capital * (leverage > 0 ? leverage : 1);
  const weight = reports.length > 0 ? 1 / reports.length : 0;
  const capitalPerStrategy = reports.length > 0 ? grossCapital / reports.length : 0;

  return {
    allocations: reports.map((report) => ({
      id: report.id,
      name: (report.strategy_name ?? report.id).replace("example.", ""),
      instruments: report.instruments.join(", "),
      sharpe: readMetric(report.best_metrics, "sharpe"),
      totalReturn: readMetric(report.best_metrics, "total_return"),
      weight,
      capital: capitalPerStrategy,
    })),
    grossCapital,
    capitalPerStrategy,
    averageSharpe: averageMetricFromSummaries(reports, "sharpe"),
    averageReturn: scaleMetric(averageMetricFromSummaries(reports, "total_return"), leverage),
    averageWinRate: averageMetricFromSummaries(reports, "win_rate"),
    averageAlpha: averageMetricFromSummaries(reports, "alpha"),
    averageBeta: averageMetricFromSummaries(reports, "beta"),
    marginMode: leverage > 1 ? "Margin enabled" : "Cash only",
  };
}

function averageMetricFromSummaries(reports: ResearchSummary[], key: string): number | null {
  const values = reports
    .map((report) => readMetric(report.best_metrics, key))
    .filter((value): value is number => value !== null);
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function readMetric(metrics: Record<string, number> | null | undefined, key: string): number | null {
  if (!metrics || metrics[key] === undefined || metrics[key] === null) {
    return null;
  }
  return Number(metrics[key]);
}

function scaleMetric(value: number | null, leverage: number): number | null {
  if (value === null) return null;
  return value * (leverage > 0 ? leverage : 1);
}

function SignalCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "emerald" | "amber" | "cyan" | "violet";
}) {
  const toneClasses =
    tone === "emerald"
      ? "border-emerald-300/20 bg-emerald-400/10 text-emerald-50"
      : tone === "amber"
        ? "border-amber-300/20 bg-amber-400/10 text-amber-50"
        : tone === "violet"
          ? "border-violet-300/20 bg-violet-400/10 text-violet-50"
          : "border-cyan-300/20 bg-cyan-400/10 text-cyan-50";

  return (
    <div className={`rounded-2xl border px-4 py-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}

function MetricPanel({
  label,
  value,
  compact = false,
}: {
  label: string;
  value: string;
  compact?: boolean;
}) {
  return (
    <div className={`rounded-2xl border border-white/10 bg-black/20 ${compact ? "px-3 py-3" : "px-4 py-4"}`}>
      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className={`mt-2 font-semibold text-white ${compact ? "text-sm" : "text-lg"}`}>{value}</p>
    </div>
  );
}

function ComparisonRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-zinc-400">{label}</span>
      <span>{value}</span>
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

function formatMetric(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    return numeric.toFixed(3);
  }
  return String(value);
}

function formatMetricOrPending(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Benchmark feed";
  }
  return formatMetric(value);
}

function formatPercent(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return `${(numeric * 100).toFixed(2)}%`;
}

function formatConfig(config: Record<string, unknown>): string {
  const entries = Object.entries(config);
  if (entries.length === 0) {
    return "{}";
  }
  return entries.map(([key, value]) => `${key}=${String(value)}`).join(", ");
}
