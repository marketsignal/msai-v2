"use client";

import { useEffect, useMemo, useState } from "react";

import { ActiveStrategies } from "@/components/dashboard/active-strategies";
import { EquityChart } from "@/components/dashboard/equity-chart";
import { PortfolioSummary } from "@/components/dashboard/portfolio-summary";
import { RecentTrades } from "@/components/dashboard/recent-trades";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type AccountSummary = {
  net_liquidation: number;
  equity_with_loan_value: number;
  buying_power: number;
  margin_used: number;
  initial_margin_requirement: number;
  maintenance_margin_requirement: number;
  available_funds: number;
  excess_liquidity: number;
  sma: number;
  gross_position_value: number;
  cushion: number;
  unrealized_pnl: number;
};

type LiveStatus = {
  id: string;
  strategy: string;
  status: "running" | "stopped" | "error" | "starting" | "liquidating";
  daily_pnl: number;
  open_positions?: number;
  open_orders?: number;
};

type LiveTrade = {
  id: string;
  executed_at: string;
  instrument: string;
  side: string;
  quantity: number;
  price: number;
  pnl: number;
};

type PortfolioRun = {
  id: string;
  status: string;
  portfolio_name: string;
  metrics?: Record<string, number> | null;
  series?: Array<{ timestamp: string; equity: number }>;
};

type GraduationCandidate = {
  id: string;
  stage: string;
};

export default function DashboardPage() {
  const { token } = useAuth();
  const [summary, setSummary] = useState<AccountSummary>({
    net_liquidation: 0,
    equity_with_loan_value: 0,
    buying_power: 0,
    margin_used: 0,
    initial_margin_requirement: 0,
    maintenance_margin_requirement: 0,
    available_funds: 0,
    excess_liquidity: 0,
    sma: 0,
    gross_position_value: 0,
    cushion: 0,
    unrealized_pnl: 0,
  });
  const [strategies, setStrategies] = useState<LiveStatus[]>([]);
  const [trades, setTrades] = useState<LiveTrade[]>([]);
  const [portfolioRuns, setPortfolioRuns] = useState<PortfolioRun[]>([]);
  const [candidates, setCandidates] = useState<GraduationCandidate[]>([]);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    if (!token) {
      return;
    }

    async function load() {
      const [accountResult, statusResult, tradesResult, portfolioResult, candidatesResult] =
        await Promise.allSettled([
          apiFetch<AccountSummary>("/api/v1/account/summary", token),
          apiFetch<LiveStatus[]>("/api/v1/live/status", token),
          apiFetch<LiveTrade[]>("/api/v1/live/trades", token),
          apiFetch<PortfolioRun[]>("/api/v1/portfolios/runs", token),
          apiFetch<GraduationCandidate[]>("/api/v1/graduation/candidates", token),
        ]);

      if (accountResult.status === "fulfilled") {
        setSummary(accountResult.value);
      }
      if (statusResult.status === "fulfilled") {
        setStrategies(statusResult.value);
      }
      if (tradesResult.status === "fulfilled") {
        setTrades(tradesResult.value);
      }
      if (portfolioResult.status === "fulfilled") {
        setPortfolioRuns(portfolioResult.value);
      }
      if (candidatesResult.status === "fulfilled") {
        setCandidates(candidatesResult.value);
      }

      const failures = [accountResult, statusResult, tradesResult, portfolioResult, candidatesResult].filter(
        (result) => result.status === "rejected",
      );
      setLoadError(
        failures.length > 0
          ? `${failures.length} dashboard data source${failures.length === 1 ? "" : "s"} failed to load.`
          : "",
      );
    }

    void load();
  }, [token]);

  const latestCompletedRun = useMemo(
    () => portfolioRuns.find((run) => run.status === "completed" && (run.series?.length ?? 0) > 0) ?? null,
    [portfolioRuns],
  );
  const equity = useMemo(
    () => latestCompletedRun?.series?.map((point) => ({ timestamp: point.timestamp, value: point.equity })) ?? [],
    [latestCompletedRun],
  );

  const marginUtilization = summary.buying_power > 0 ? summary.margin_used / summary.buying_power : 0;
  const runningStrategies = strategies.filter((item) => item.status === "running");
  const startingStrategies = strategies.filter((item) => item.status === "starting" || item.status === "liquidating");
  const totalOpenPositions = strategies.reduce((total, strategy) => total + Number(strategy.open_positions ?? 0), 0);
  const totalOpenOrders = strategies.reduce((total, strategy) => total + Number(strategy.open_orders ?? 0), 0);
  const paperCandidates = candidates.filter((candidate) => candidate.stage.startsWith("paper")).length;
  const liveCandidates = candidates.filter((candidate) => candidate.stage.startsWith("live")).length;

  return (
    <div className="space-y-6">
      {loadError ? (
        <div className="rounded-[1.5rem] border border-amber-300/30 bg-amber-500/10 p-4 text-sm text-amber-50">
          {loadError} The desk stays live, but panels with missing upstream data now show honest empty state instead of
          demo values.
        </div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[1.35fr_0.95fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(45,212,191,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6 shadow-[0_24px_90px_rgba(0,0,0,0.35)]">
          <p className="text-[11px] uppercase tracking-[0.3em] text-cyan-200/80">Operator Loop</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">Run research, shape portfolio risk, then deploy with confidence.</h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            This desk is designed for the exact workflow you described: drive strategy iteration from Codex or the API,
            watch the portfolio and live allocations on a second monitor, and keep research, promotion, and execution on
            the same control surface.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-3">
            <HeroMetric label="Running now" value={String(runningStrategies.length)} tone="emerald" />
            <HeroMetric label="Starting / Liquidating" value={String(startingStrategies.length)} tone="amber" />
            <HeroMetric label="Open risk slots" value={`${totalOpenPositions} positions / ${totalOpenOrders} orders`} tone="violet" />
          </div>
          {latestCompletedRun ? (
            <p className="mt-4 text-sm text-zinc-400">
              Showing real portfolio analytics from <span className="text-white">{latestCompletedRun.portfolio_name}</span>{" "}
              for the equity curve and allocation lens.
            </p>
          ) : (
            <p className="mt-4 text-sm text-zinc-500">
              No completed portfolio run is available yet. The desk will show real portfolio series here as soon as you
              backtest one from the Portfolio workspace.
            </p>
          )}
        </div>

        <div className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Portfolio State</p>
          <div className="mt-4 space-y-4">
            <DeskRow label="Equity with loan value" value={money(summary.equity_with_loan_value)} note="Broker equity basis for margin" />
            <DeskRow label="Buying power" value={money(summary.buying_power)} note="Dry powder for new deployments" />
            <DeskRow label="Available funds" value={money(summary.available_funds)} note="Cash after current commitments" />
            <DeskRow label="Excess liquidity" value={money(summary.excess_liquidity)} note="Buffer before margin pressure" />
            <DeskRow
              label="Margin utilization"
              value={`${(marginUtilization * 100).toFixed(1)}%`}
              note="Current capital intensity"
            />
            <DeskRow
              label="Initial / Maintenance"
              value={`${money(summary.initial_margin_requirement)} / ${money(summary.maintenance_margin_requirement)}`}
              note="Broker margin requirements"
            />
            <DeskRow
              label="Intraday P&L"
              value={money(summary.unrealized_pnl)}
              note="Current mark-to-market"
              positive={summary.unrealized_pnl >= 0}
            />
          </div>
        </div>
      </section>

      <PortfolioSummary
        totalValue={summary.net_liquidation}
        dailyPnl={summary.unrealized_pnl}
        totalReturn={summary.net_liquidation > 0 ? summary.unrealized_pnl / summary.net_liquidation : 0}
        activeStrategies={runningStrategies.length}
      />

      <section className="grid gap-4 lg:grid-cols-4">
        <SignalTile
          title="Risk posture"
          value={marginUtilization > 0.4 ? "Aggressive" : "Controlled"}
          note={`${(marginUtilization * 100).toFixed(1)}% of buying power in use`}
          tone={marginUtilization > 0.4 ? "amber" : "cyan"}
        />
        <SignalTile
          title="Research to live"
          value="API-first"
          note="Same engine for CLI, agents, and UI"
          tone="violet"
        />
        <SignalTile
          title="Execution lane"
          value="Interactive Brokers"
          note="Streaming + execution venue"
          tone="emerald"
        />
        <SignalTile
          title="Historical lane"
          value="Databento"
          note="Backtests, sweeps, and walk-forward"
          tone="cyan"
        />
        <SignalTile
          title="Paper candidates"
          value={String(paperCandidates)}
          note="Graduation queue for paper"
          tone="violet"
        />
        <SignalTile
          title="Live candidates"
          value={String(liveCandidates)}
          note="Strategies cleared toward live"
          tone="emerald"
        />
      </section>

      <div className="grid gap-6 xl:grid-cols-[1.35fr_1fr]">
        <EquityChart data={equity} />
        <div className="space-y-6">
          <ActiveStrategies
            items={strategies.map((strategy) => ({
              id: strategy.id,
              name: strategy.strategy,
              status: strategy.status,
              dailyPnl: strategy.daily_pnl,
            }))}
          />
          <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.94),rgba(8,12,18,0.78))] p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-white">Capital Allocation Frame</h2>
                <p className="mt-1 text-sm text-zinc-400">What the portfolio can support right now.</p>
              </div>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <MiniStat label="Per running strategy" value={money(runningStrategies.length ? summary.available_funds / runningStrategies.length : summary.available_funds)} />
              <MiniStat label="Reserve capital" value={money(summary.available_funds * 0.25)} />
              <MiniStat label="Open positions" value={String(totalOpenPositions)} />
              <MiniStat label="Open orders" value={String(totalOpenOrders)} />
              <MiniStat label="SMA" value={money(summary.sma)} />
              <MiniStat label="Cushion" value={`${(summary.cushion * 100).toFixed(1)}%`} />
              <MiniStat
                label="Latest portfolio Sharpe"
                value={latestCompletedRun?.metrics?.sharpe?.toFixed(2) ?? "N/A"}
              />
              <MiniStat
                label="Latest portfolio Sortino"
                value={latestCompletedRun?.metrics?.sortino?.toFixed(2) ?? "N/A"}
              />
            </div>
          </section>
        </div>
      </div>

      <RecentTrades
        items={trades.map((item) => ({
          id: item.id,
          timestamp: item.executed_at,
          instrument: item.instrument,
          side: item.side,
          quantity: item.quantity,
          price: item.price,
          pnl: item.pnl,
        }))}
      />
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

function HeroMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "emerald" | "amber" | "violet";
}) {
  const toneClasses =
    tone === "emerald"
      ? "border-emerald-300/20 bg-emerald-400/10 text-emerald-50"
      : tone === "amber"
        ? "border-amber-300/20 bg-amber-400/10 text-amber-50"
        : "border-violet-300/20 bg-violet-400/10 text-violet-50";

  return (
    <div className={`rounded-2xl border px-4 py-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}

function DeskRow({
  label,
  value,
  note,
  positive = true,
}: {
  label: string;
  value: string;
  note: string;
  positive?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-sm text-zinc-300">{label}</p>
          <p className="mt-1 text-xs text-zinc-500">{note}</p>
        </div>
        <p className={`text-lg font-semibold ${positive ? "text-white" : "text-rose-300"}`}>{value}</p>
      </div>
    </div>
  );
}

function SignalTile({
  title,
  value,
  note,
  tone,
}: {
  title: string;
  value: string;
  note: string;
  tone: "cyan" | "amber" | "violet" | "emerald";
}) {
  const toneClasses =
    tone === "amber"
      ? "border-amber-300/20 bg-amber-400/10"
      : tone === "violet"
        ? "border-violet-300/20 bg-violet-400/10"
        : tone === "emerald"
          ? "border-emerald-300/20 bg-emerald-400/10"
          : "border-cyan-300/20 bg-cyan-400/10";

  return (
    <article className={`rounded-[1.35rem] border p-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.22em] text-white/55">{title}</p>
      <p className="mt-3 text-xl font-semibold text-white">{value}</p>
      <p className="mt-2 text-sm text-zinc-300">{note}</p>
    </article>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className="mt-3 text-lg font-semibold text-white">{value}</p>
    </div>
  );
}
