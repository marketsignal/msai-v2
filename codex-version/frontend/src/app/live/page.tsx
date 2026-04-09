"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { KillSwitch } from "@/components/live/kill-switch";
import { PositionsTable } from "@/components/live/positions-table";
import { StrategyStatus } from "@/components/live/strategy-status";
import { useAuth } from "@/lib/auth";
import { isLiveStreamEnabled } from "@/lib/auth-mode";
import { apiFetch } from "@/lib/api";

type Deployment = {
  id: string;
  strategy: string;
  status: string;
  started_at?: string;
  daily_pnl?: number;
  open_positions?: number;
  open_orders?: number;
  updated_at?: string;
};

type Position = {
  deployment_id?: string;
  instrument: string;
  quantity: number;
  avg_price: number;
  current_price?: number;
  unrealized_pnl: number;
  market_value: number;
};

type StrategySummary = { id: string; name: string };
type StrategyDetail = { default_config?: Record<string, unknown> };

type LiveOrder = {
  deployment_id?: string;
  instrument?: string;
  side?: string;
  quantity?: number;
  status?: string;
  order_type?: string;
  price?: number;
  ts_last?: string;
};

type LiveTrade = {
  id?: string;
  deployment_id?: string;
  instrument: string;
  side?: string;
  quantity?: number;
  price?: number;
  pnl?: number;
  executed_at?: string;
};

type RiskSnapshot = {
  halted?: boolean;
  reason?: string | null;
  updated_at?: string | null;
  current_pnl?: number;
  notional_exposure?: number;
  portfolio_value?: number;
  margin_used?: number;
  position_count?: number;
};

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

type BrokerPosition = {
  account_id?: string | null;
  instrument: string;
  quantity?: number;
  avg_price?: number | null;
  market_value?: number | null;
  unrealized_pnl?: number | null;
};

type BrokerOrder = {
  account_id?: string | null;
  instrument: string;
  status?: string | null;
  side?: string | null;
  quantity?: number;
  remaining?: number;
};

type BrokerSnapshot = {
  connected: boolean;
  mock_mode: boolean;
  generated_at: string;
  positions: BrokerPosition[];
  open_orders: BrokerOrder[];
};

type RiskHistoryPoint = {
  timestamp: string;
  current_pnl: number;
  portfolio_value: number;
};

type StreamEvent = {
  type: string;
  scope?: string | null;
  generated_at?: string | null;
  summary: string;
};

type StreamPayload = {
  type?: string;
  scope?: string | null;
  generated_at?: string | null;
  data?: unknown;
  message?: string;
};

type PromotionDraft = {
  id: string;
  report_id: string;
  strategy_id: string;
  strategy_name: string;
  instruments: string[];
  config: Record<string, unknown>;
  selection: {
    kind: string;
    result_index?: number | null;
    window_index?: number | null;
  };
};

type GraduationCandidate = {
  id: string;
  stage: string;
  strategy_id: string;
  strategy_name: string;
  instruments: string[];
  config: Record<string, unknown>;
  paper_trading: boolean;
  live_url: string;
  portfolio_url: string;
};

export default function LivePage() {
  const searchParams = useSearchParams();
  const { token } = useAuth();
  const promotionId = searchParams.get("promotion_id");
  const candidateId = searchParams.get("candidate_id");
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<LiveOrder[]>([]);
  const [trades, setTrades] = useState<LiveTrade[]>([]);
  const [risk, setRisk] = useState<RiskSnapshot>({});
  const [accountSummary, setAccountSummary] = useState<AccountSummary>({
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
  const [brokerSnapshot, setBrokerSnapshot] = useState<BrokerSnapshot | null>(null);
  const [riskHistory, setRiskHistory] = useState<RiskHistoryPoint[]>([]);
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [instrumentsInput, setInstrumentsInput] = useState("AAPL.XNAS");
  const [configText, setConfigText] = useState("{}");
  const [deploymentMode, setDeploymentMode] = useState<"paper" | "live">("paper");
  const [startError, setStartError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [starting, setStarting] = useState(false);
  const [promotionDraft, setPromotionDraft] = useState<PromotionDraft | null>(null);
  const [candidate, setCandidate] = useState<GraduationCandidate | null>(null);
  const [streamState, setStreamState] = useState<"connected" | "reconnecting" | "disabled">(
    isLiveStreamEnabled() ? "reconnecting" : "disabled",
  );
  const [streamEvents, setStreamEvents] = useState<StreamEvent[]>([]);

  const recordRiskSnapshot = useCallback((snapshot: RiskSnapshot) => {
    const timestamp = snapshot.updated_at ?? new Date().toISOString();
    setRiskHistory((current) => {
      const nextPoint = {
        timestamp,
        current_pnl: Number(snapshot.current_pnl ?? 0),
        portfolio_value: Number(snapshot.portfolio_value ?? 0),
      };
      if (current.at(-1)?.timestamp === nextPoint.timestamp) {
        return [...current.slice(0, -1), nextPoint];
      }
      return [...current, nextPoint].slice(-120);
    });
  }, []);

  const load = useCallback(async () => {
    if (!token) return;

    const [statusResult, positionsResult, strategiesResult, riskResult, ordersResult, tradesResult, accountResult, brokerResult] =
      await Promise.allSettled([
        apiFetch<Deployment[]>("/api/v1/live/status", token),
        apiFetch<Position[]>("/api/v1/live/positions", token),
        apiFetch<StrategySummary[]>("/api/v1/strategies/", token),
        apiFetch<RiskSnapshot>("/api/v1/live/risk-status", token),
        apiFetch<LiveOrder[]>("/api/v1/live/orders", token),
        apiFetch<LiveTrade[]>("/api/v1/live/trades", token),
        apiFetch<AccountSummary>("/api/v1/account/summary", token),
        apiFetch<BrokerSnapshot>("/api/v1/account/snapshot", token),
      ]);

    if (statusResult.status === "fulfilled") {
      setDeployments(statusResult.value);
    }
    if (positionsResult.status === "fulfilled") {
      setPositions(positionsResult.value);
    }
    if (ordersResult.status === "fulfilled") {
      setOrders(ordersResult.value);
    }
    if (tradesResult.status === "fulfilled") {
      setTrades(tradesResult.value);
    }
    if (riskResult.status === "fulfilled") {
      setRisk(riskResult.value);
      recordRiskSnapshot(riskResult.value);
    }
    if (accountResult.status === "fulfilled") {
      setAccountSummary(accountResult.value);
    }
    if (brokerResult.status === "fulfilled") {
      setBrokerSnapshot(brokerResult.value);
    }
    if (strategiesResult.status === "fulfilled") {
      setStrategies(strategiesResult.value);
      if (
        strategiesResult.value[0]?.id &&
        (!selectedStrategyId || !strategiesResult.value.some((strategy) => strategy.id === selectedStrategyId))
      ) {
        setSelectedStrategyId(strategiesResult.value[0].id);
      }
    }

    const failures = [statusResult, positionsResult, strategiesResult, riskResult, ordersResult, tradesResult, accountResult, brokerResult].filter(
      (result) => result.status === "rejected",
    );
    setLoadError(
      failures.length > 0
        ? `${failures.length} live data source${failures.length === 1 ? "" : "s"} failed to load.`
        : "",
    );
  }, [recordRiskSnapshot, selectedStrategyId, token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!token || !selectedStrategyId) return;
    if (promotionId || candidateId) return;
    if (promotionDraft?.strategy_id === selectedStrategyId || candidate?.strategy_id === selectedStrategyId) return;

    async function loadDefaultConfig() {
      try {
        const detail = await apiFetch<StrategyDetail>(`/api/v1/strategies/${selectedStrategyId}`, token);
        setConfigText(JSON.stringify(detail.default_config ?? {}, null, 2));
      } catch {
        setConfigText("{}");
      }
    }

    void loadDefaultConfig();
  }, [candidate, candidateId, promotionDraft, promotionId, selectedStrategyId, token]);

  useEffect(() => {
    if (!token || !candidateId) return;

    async function loadCandidate() {
      try {
        const detail = await apiFetch<GraduationCandidate>(`/api/v1/graduation/candidates/${candidateId}`, token);
        setCandidate(detail);
        setPromotionDraft(null);
        setSelectedStrategyId(detail.strategy_id);
        setInstrumentsInput(detail.instruments.join(","));
        setConfigText(JSON.stringify(detail.config ?? {}, null, 2));
        setDeploymentMode(detail.paper_trading ? "paper" : "live");
        setStartError("");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load graduation candidate";
        setStartError(message);
      }
    }

    void loadCandidate();
  }, [candidateId, token]);

  useEffect(() => {
    if (!token || !promotionId || candidateId) return;

    async function loadPromotionDraft() {
      try {
        const draft = await apiFetch<PromotionDraft>(`/api/v1/research/promotions/${promotionId}`, token);
        setPromotionDraft(draft);
        setCandidate(null);
        setSelectedStrategyId(draft.strategy_id);
        setInstrumentsInput(draft.instruments.join(","));
        setConfigText(JSON.stringify(draft.config ?? {}, null, 2));
        setDeploymentMode("paper");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load promotion draft";
        setStartError(message);
      }
    }

    void loadPromotionDraft();
  }, [candidateId, promotionId, token]);

  useEffect(() => {
    if (!token || !isLiveStreamEnabled()) {
      setStreamState(isLiveStreamEnabled() ? "reconnecting" : "disabled");
      return;
    }

    const ws = new WebSocket(
      `${(process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace("http", "ws")}/api/v1/live/stream`,
    );

    ws.addEventListener("open", () => {
      setStreamState("connected");
      ws.send(token);
    });

    ws.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data) as StreamPayload;
        const summary = summarizeStreamEvent(payload);
        setStreamEvents((current) => [
          {
            type: payload.type ?? "unknown",
            scope: payload.scope ?? undefined,
            generated_at: payload.generated_at ?? undefined,
            summary,
          },
          ...current,
        ].slice(0, 20));

        if (payload.type === "risk.snapshot" && isObject(payload.data)) {
          const snapshot = payload.data as RiskSnapshot;
          setRisk(snapshot);
          recordRiskSnapshot(snapshot);
        }
        if (payload.type === "positions.snapshot" && Array.isArray(payload.data)) {
          setPositions((current) => mergeScopedRows(current, payload.scope, payload.data as Position[]));
        }
        if (payload.type === "orders.snapshot" && Array.isArray(payload.data)) {
          setOrders((current) => mergeScopedRows(current, payload.scope, payload.data as LiveOrder[]));
        }
        if (payload.type === "trades.snapshot" && Array.isArray(payload.data)) {
          setTrades((current) => mergeScopedRows(current, payload.scope, payload.data as LiveTrade[]).slice(0, 50));
        }
        if (payload.type === "status.snapshot" && isObject(payload.data) && payload.scope) {
          setDeployments((current) =>
            current.map((deployment) =>
              deployment.id === payload.scope
                ? {
                    ...deployment,
                    status: String((payload.data as Record<string, unknown>).status ?? deployment.status),
                    daily_pnl: Number((payload.data as Record<string, unknown>).daily_pnl ?? deployment.daily_pnl ?? 0),
                    open_positions: Number(
                      (payload.data as Record<string, unknown>).open_positions ?? deployment.open_positions ?? 0,
                    ),
                    open_orders: Number(
                      (payload.data as Record<string, unknown>).open_orders ?? deployment.open_orders ?? 0,
                    ),
                    updated_at: String(
                      (payload.data as Record<string, unknown>).updated_at ?? deployment.updated_at ?? "",
                    ),
                  }
                : deployment,
            ),
          );
        }
      } catch {
        setStreamEvents((current) => [
          {
            type: "stream.raw",
            summary: String(event.data),
            generated_at: new Date().toISOString(),
          },
          ...current,
        ].slice(0, 20));
      }
    });

    ws.addEventListener("close", () => {
      setStreamState("reconnecting");
    });

    return () => {
      ws.close();
    };
  }, [recordRiskSnapshot, token]);

  const livePnl = risk.current_pnl ?? deployments.reduce((sum, row) => sum + Number(row.daily_pnl ?? 0), 0);
  const exposure = risk.notional_exposure ?? positions.reduce((sum, row) => sum + Math.abs(row.market_value), 0);
  const totalMarketValue = positions.reduce((sum, row) => sum + row.market_value, 0);
  const activeDeployments = deployments.filter((deployment) =>
    ["running", "starting", "liquidating"].includes(deployment.status),
  );
  const riskSeries = riskHistory.filter((point) => Number.isFinite(point.portfolio_value) || Number.isFinite(point.current_pnl));

  async function startDeployment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !selectedStrategyId) return;

    const instruments = instrumentsInput
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (instruments.length === 0) {
      setStartError("At least one instrument is required.");
      return;
    }

    let config: Record<string, unknown> = {};
    try {
      config = JSON.parse(configText) as Record<string, unknown>;
    } catch {
      setStartError("Config JSON is invalid.");
      return;
    }

    try {
      setStarting(true);
      setStartError("");
      await apiFetch<{ deployment_id: string }>("/api/v1/live/start", token, {
        method: "POST",
        body: JSON.stringify({
          strategy_id: selectedStrategyId,
          config,
          instruments,
          paper_trading: deploymentMode === "paper",
        }),
      });

      if (candidate) {
        const nextStage = deploymentMode === "paper" ? "paper_running" : "live_running";
        try {
          const updated = await apiFetch<GraduationCandidate>(
            `/api/v1/graduation/candidates/${candidate.id}/stage`,
            token,
            {
              method: "POST",
              body: JSON.stringify({ stage: nextStage }),
            },
          );
          setCandidate(updated);
        } catch {
          // Keep live start successful even if stage bookkeeping fails.
        }
      }

      await load();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Start failed";
      setStartError(message);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-6">
      {loadError ? (
        <div className="rounded-[1.5rem] border border-amber-300/30 bg-amber-500/10 p-4 text-sm text-amber-50">
          {loadError} This desk now shows honest empty state if upstream services are unavailable instead of rendering demo
          positions or fake fills.
        </div>
      ) : null}

      {candidate ? (
        <div className="rounded-[1.5rem] border border-cyan-300/30 bg-[linear-gradient(180deg,rgba(34,211,238,0.12),rgba(34,211,238,0.06))] p-5">
          <p className="text-sm font-semibold text-cyan-100">Graduation candidate loaded</p>
          <p className="mt-2 text-sm leading-6 text-cyan-50/90">
            {candidate.strategy_name} is staged as <span className="font-medium">{candidate.stage.replace("_", " ")}</span>{" "}
            and prefilled for {deploymentMode} deployment.
          </p>
          <div className="mt-4 flex flex-wrap gap-3">
            <Link href="/graduation" className="inline-flex rounded-2xl border border-cyan-300/30 px-4 py-2 text-sm text-cyan-50">
              Return to Graduation
            </Link>
            <Link href={candidate.portfolio_url} className="inline-flex rounded-2xl border border-violet-300/30 px-4 py-2 text-sm text-violet-100">
              Open Portfolio Lab
            </Link>
          </div>
        </div>
      ) : promotionDraft ? (
        <div className="rounded-[1.5rem] border border-cyan-300/30 bg-[linear-gradient(180deg,rgba(34,211,238,0.12),rgba(34,211,238,0.06))] p-5">
          <p className="text-sm font-semibold text-cyan-100">Paper promotion loaded</p>
          <p className="mt-2 text-sm leading-6 text-cyan-50/90">
            Research report {promotionDraft.report_id} prefilled {promotionDraft.strategy_name} for paper deployment.
          </p>
          <Link href="/research" className="mt-4 inline-flex rounded-2xl border border-cyan-300/30 px-4 py-2 text-sm text-cyan-50">
            Return to Research
          </Link>
        </div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(45,212,191,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-cyan-200/80">Live Command Center</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">
            Watch the deployment loop the same way you’d run it from the terminal.
          </h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            The UI mirrors the API-first workflow: deploy promoted strategies, monitor open risk, watch streaming state,
            and kill or stop gracefully without needing a separate operator tool.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <SignalCard
              label="Stream"
              value={streamState === "disabled" ? "disabled for tests" : streamState}
              tone={
                streamState === "connected"
                  ? "emerald"
                  : streamState === "disabled"
                    ? "neutral"
                    : "amber"
              }
            />
            <SignalCard label="Deployments" value={String(activeDeployments.length)} tone="cyan" />
            <SignalCard label="Open positions" value={String(positions.length)} tone="violet" />
            <SignalCard label="Risk state" value={risk.halted ? "Halted" : "Active"} tone={risk.halted ? "rose" : "emerald"} />
          </div>
          <p className="mt-4 text-sm text-zinc-400">
            Stream status:{" "}
            <span
              className={
                streamState === "connected"
                  ? "text-emerald-300"
                  : streamState === "disabled"
                    ? "text-zinc-300"
                    : "text-amber-300"
              }
            >
              {streamState === "disabled" ? "disabled for tests" : streamState}
            </span>
          </p>
        </div>

        <div className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Risk Snapshot</p>
              <h3 className="mt-2 text-xl font-semibold text-white">Portfolio guardrails</h3>
            </div>
            <KillSwitch
              onKillAll={() => {
                if (!token) return;
                void apiFetch<{ stopped: number }>("/api/v1/live/kill-all", token, { method: "POST" }).then(() => load());
              }}
            />
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <InfoPanel label="Current P&L" value={money(livePnl)} positive={livePnl >= 0} />
            <InfoPanel label="Notional exposure" value={money(exposure)} />
            <InfoPanel label="Portfolio value" value={money(risk.portfolio_value ?? totalMarketValue)} />
            <InfoPanel label="Margin used" value={money(risk.margin_used ?? 0)} />
          </div>
          <div className="mt-4 rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">Realtime risk trace</p>
              <p className="text-xs text-zinc-500">{riskSeries.length} points</p>
            </div>
            <div className="mt-4 h-48 min-w-0">
              {riskSeries.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={riskSeries}>
                    <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
                    <XAxis dataKey="timestamp" hide />
                    <YAxis stroke="#64748b" tickLine={false} axisLine={false} />
                    <Tooltip
                      labelFormatter={(value) => new Date(value).toLocaleTimeString()}
                      formatter={(value: number, key) => [money(value), key === "portfolio_value" ? "Portfolio Value" : "P&L"]}
                      contentStyle={{
                        backgroundColor: "#09111b",
                        border: "1px solid rgba(255,255,255,0.1)",
                        borderRadius: "16px",
                      }}
                    />
                    <Line type="monotone" dataKey="portfolio_value" stroke="#22d3ee" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="current_pnl" stroke="#f59e0b" strokeWidth={1.8} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center rounded-[1.1rem] border border-dashed border-white/10 text-center text-sm text-zinc-500">
                  Waiting for live or polled risk snapshots.
                </div>
              )}
            </div>
          </div>
          <div className="mt-4 rounded-2xl border border-white/10 bg-black/20 p-4">
            <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">Operator note</p>
            <p className="mt-2 text-sm text-zinc-300">
              {risk.reason
                ? risk.reason
                : "Use paper promotions to validate fills and streaming state first, then promote the same strategy code toward live capital."}
            </p>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Broker Account</p>
              <h2 className="mt-2 text-lg font-semibold text-white">Interactive Brokers status</h2>
            </div>
            <span
              className={`rounded-full border px-3 py-1 text-xs ${
                brokerSnapshot?.connected
                  ? "border-emerald-300/30 bg-emerald-500/10 text-emerald-200"
                  : "border-amber-300/30 bg-amber-500/10 text-amber-100"
              }`}
            >
              {brokerSnapshot?.connected ? "Connected" : "Disconnected"}
            </span>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <InfoPanel label="Net liquidation" value={money(accountSummary.net_liquidation)} />
            <InfoPanel label="Equity w/ loan" value={money(accountSummary.equity_with_loan_value)} />
            <InfoPanel label="Buying power" value={money(accountSummary.buying_power)} />
            <InfoPanel label="Available funds" value={money(accountSummary.available_funds)} />
            <InfoPanel label="Excess liquidity" value={money(accountSummary.excess_liquidity)} />
            <InfoPanel
              label="Init / Maint margin"
              value={`${money(accountSummary.initial_margin_requirement)} / ${money(accountSummary.maintenance_margin_requirement)}`}
            />
            <InfoPanel label="Gross position value" value={money(accountSummary.gross_position_value)} />
            <InfoPanel label="SMA / Cushion" value={`${money(accountSummary.sma)} / ${(accountSummary.cushion * 100).toFixed(1)}%`} />
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <InfoPanel label="Broker positions" value={String(brokerSnapshot?.positions.length ?? 0)} />
            <InfoPanel label="Broker open orders" value={String(brokerSnapshot?.open_orders.length ?? 0)} />
            <InfoPanel label="Updated" value={brokerSnapshot?.generated_at ? new Date(brokerSnapshot.generated_at).toLocaleTimeString() : "N/A"} />
          </div>
        </section>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Broker Truth</p>
              <h2 className="mt-2 text-lg font-semibold text-white">IB positions and working orders</h2>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              {brokerSnapshot?.mock_mode ? "mock" : "live broker"}
            </span>
          </div>
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium text-white">Positions</p>
                <p className="text-xs text-zinc-500">{brokerSnapshot?.positions.length ?? 0}</p>
              </div>
              <div className="mt-3 space-y-3">
                {(brokerSnapshot?.positions.length ?? 0) === 0 ? (
                  <p className="text-sm text-zinc-500">No broker positions.</p>
                ) : (
                  brokerSnapshot?.positions.slice(0, 6).map((position, index) => (
                    <div key={`${position.instrument}-${index}`} className="rounded-xl border border-white/10 px-3 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <p className="text-sm font-medium text-white">{position.instrument}</p>
                        <p className="text-sm text-zinc-300">{Number(position.quantity ?? 0).toFixed(2)}</p>
                      </div>
                      <p className="mt-1 text-xs text-zinc-500">
                        MV {money(position.market_value ?? 0)} · UPNL {money(position.unrealized_pnl ?? 0)}
                      </p>
                    </div>
                  ))
                )}
              </div>
            </div>
            <div className="rounded-2xl border border-white/10 bg-black/20 p-4">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium text-white">Open orders</p>
                <p className="text-xs text-zinc-500">{brokerSnapshot?.open_orders.length ?? 0}</p>
              </div>
              <div className="mt-3 space-y-3">
                {(brokerSnapshot?.open_orders.length ?? 0) === 0 ? (
                  <p className="text-sm text-zinc-500">No broker open orders.</p>
                ) : (
                  brokerSnapshot?.open_orders.slice(0, 6).map((order, index) => (
                    <div key={`${order.instrument}-${order.status ?? "unknown"}-${index}`} className="rounded-xl border border-white/10 px-3 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <p className="text-sm font-medium text-white">{order.instrument}</p>
                        <p className="text-sm text-zinc-300">{order.status ?? "unknown"}</p>
                      </div>
                      <p className="mt-1 text-xs text-zinc-500">
                        {order.side ?? "order"} · {Number(order.quantity ?? 0).toFixed(2)} total · {Number(order.remaining ?? 0).toFixed(2)} remaining
                      </p>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </section>
      </section>

      <div className="grid gap-6 xl:grid-cols-[1.05fr_0.95fr]">
        <form
          className="space-y-4 rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5"
          onSubmit={(event) => {
            void startDeployment(event);
          }}
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-white">Deploy Strategy</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Launch the same promoted config you approved in research, or edit it manually for validation.
              </p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              API-first
            </span>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Strategy</span>
              <select
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                value={selectedStrategyId}
                onChange={(event) => setSelectedStrategyId(event.target.value)}
              >
                {strategies.map((strategy) => (
                  <option key={strategy.id} value={strategy.id}>
                    {strategy.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-2 text-sm text-zinc-300 md:col-span-2">
              <span>Instruments</span>
              <input
                aria-label="Instruments"
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                value={instrumentsInput}
                onChange={(event) => setInstrumentsInput(event.target.value)}
                placeholder="AAPL.XNAS,MSFT.XNAS"
              />
            </label>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Deployment Mode</span>
              <select
                aria-label="Deployment Mode"
                value={deploymentMode}
                onChange={(event) => setDeploymentMode(event.target.value as "paper" | "live")}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              >
                <option value="paper">Paper</option>
                <option value="live">Live</option>
              </select>
            </label>
            <div className="rounded-[1.25rem] border border-white/10 bg-black/20 px-4 py-4">
              <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">Source</p>
              <p className="mt-3 text-sm text-zinc-200">
                {candidate
                  ? `Graduation candidate ${candidate.id}`
                  : promotionDraft
                    ? `Research promotion ${promotionDraft.id}`
                    : "Manual API/desk launch"}
              </p>
            </div>
          </div>
          <label className="space-y-2 text-sm text-zinc-300">
            <span>Config JSON</span>
            <textarea
              aria-label="Config JSON"
              className="min-h-40 w-full rounded-[1.25rem] border border-white/10 bg-black/30 p-3 font-mono text-xs text-white"
              value={configText}
              onChange={(event) => setConfigText(event.target.value)}
            />
          </label>
          {startError ? <p className="text-sm text-rose-300">{startError}</p> : null}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-zinc-500">
              Deployment mode controls the exact same API contract you can call from `curl` or the CLI.
            </p>
            <button
              type="submit"
              disabled={starting || !selectedStrategyId}
              className="rounded-2xl border border-emerald-300/40 bg-emerald-500/20 px-4 py-3 text-sm font-medium text-emerald-100 disabled:opacity-60"
            >
              {starting ? "Starting..." : `Start ${deploymentMode === "paper" ? "Paper" : "Live"} Deployment`}
            </button>
          </div>
        </form>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-white">Realtime Event Feed</h2>
              <p className="mt-1 text-sm text-zinc-400">WebSocket snapshots and runtime events flowing through the desk.</p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              {streamEvents.length} events
            </span>
          </div>
          <div className="mt-4 space-y-3">
            {streamEvents.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-white/10 px-4 py-8 text-sm text-zinc-500">
                Waiting for live snapshots. Once the runtime stream is active, order, trade, and risk updates will appear here.
              </div>
            ) : null}
            {streamEvents.map((event, index) => (
              <div key={`${event.type}-${event.generated_at ?? index}`} className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-medium text-white">{event.type}</p>
                  <p className="text-xs text-zinc-500">
                    {event.generated_at ? new Date(event.generated_at).toLocaleTimeString() : "pending"}
                  </p>
                </div>
                <p className="mt-2 text-sm text-zinc-300">{event.summary}</p>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="grid gap-6 xl:grid-cols-[1.15fr_0.95fr]">
        <StrategyStatus
          rows={deployments}
          onStop={(id) => {
            if (!token) return;
            void apiFetch<{ status: string }>("/api/v1/live/stop", token, {
              method: "POST",
              body: JSON.stringify({ deployment_id: id }),
            }).then(() => load());
          }}
        />

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-white">Trade Tape</h2>
              <p className="mt-1 text-sm text-zinc-400">Latest fills and realized execution flow.</p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              {trades.length} fills
            </span>
          </div>
          <div className="mt-4 space-y-3">
            {trades.length === 0 ? <p className="text-sm text-zinc-500">No live trades yet.</p> : null}
            {trades.slice(0, 8).map((trade, index) => (
              <div key={`${trade.id ?? trade.instrument}-${index}`} className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-white">{trade.instrument}</p>
                    <p className="mt-1 text-xs text-zinc-500">
                      {trade.side ?? "fill"} · {trade.quantity ?? 0} @ {(trade.price ?? 0).toFixed(2)}
                    </p>
                  </div>
                  <p className={`text-sm font-medium ${(trade.pnl ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {(trade.pnl ?? 0) >= 0 ? "+" : ""}${(trade.pnl ?? 0).toFixed(2)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <PositionsTable rows={positions} />

      <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-white">Order Blotter</h2>
            <p className="mt-1 text-sm text-zinc-400">Open and recent order state from the Nautilus runtime.</p>
          </div>
          <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
            {orders.length} orders
          </span>
        </div>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[780px] text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
                <th className="pb-3">Instrument</th>
                <th className="pb-3">Deployment</th>
                <th className="pb-3">Side</th>
                <th className="pb-3">Qty</th>
                <th className="pb-3">Type</th>
                <th className="pb-3">Status</th>
                <th className="pb-3 text-right">Price</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((order, index) => (
                <tr key={`${order.instrument ?? "order"}-${order.ts_last ?? index}`} className="border-t border-white/10 text-zinc-200">
                  <td className="py-3 font-medium text-white">{order.instrument ?? "-"}</td>
                  <td className="py-3 text-zinc-400">{order.deployment_id ?? "-"}</td>
                  <td className="py-3">{order.side ?? "-"}</td>
                  <td className="py-3">{order.quantity ?? "-"}</td>
                  <td className="py-3">{order.order_type ?? "-"}</td>
                  <td className="py-3">{order.status ?? "-"}</td>
                  <td className="py-3 text-right">{order.price !== undefined ? `$${order.price.toFixed(2)}` : "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
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
  tone: "emerald" | "amber" | "cyan" | "violet" | "rose" | "neutral";
}) {
  const classes =
    tone === "emerald"
      ? "border-emerald-300/20 bg-emerald-400/10 text-emerald-50"
      : tone === "amber"
        ? "border-amber-300/20 bg-amber-400/10 text-amber-50"
        : tone === "violet"
          ? "border-violet-300/20 bg-violet-400/10 text-violet-50"
          : tone === "rose"
            ? "border-rose-300/20 bg-rose-400/10 text-rose-50"
            : tone === "neutral"
              ? "border-white/10 bg-white/5 text-zinc-50"
              : "border-cyan-300/20 bg-cyan-400/10 text-cyan-50";

  return (
    <div className={`rounded-2xl border px-4 py-4 ${classes}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}

function InfoPanel({
  label,
  value,
  positive = true,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className={`mt-3 text-lg font-semibold ${positive ? "text-white" : "text-rose-300"}`}>{value}</p>
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

function summarizeStreamEvent(payload: StreamPayload): string {
  if (payload.message) {
    return payload.message;
  }
  if (payload.type === "positions.snapshot" && Array.isArray(payload.data)) {
    return `${payload.data.length} position rows updated${payload.scope ? ` for ${payload.scope}` : ""}.`;
  }
  if (payload.type === "orders.snapshot" && Array.isArray(payload.data)) {
    return `${payload.data.length} order rows updated${payload.scope ? ` for ${payload.scope}` : ""}.`;
  }
  if (payload.type === "trades.snapshot" && Array.isArray(payload.data)) {
    return `${payload.data.length} trade rows updated${payload.scope ? ` for ${payload.scope}` : ""}.`;
  }
  if (payload.type === "status.snapshot" && isObject(payload.data)) {
    return `Deployment ${payload.scope ?? "runtime"} is ${String(payload.data.status ?? "updated")}.`;
  }
  if (payload.type === "risk.snapshot" && isObject(payload.data)) {
    return `Risk snapshot updated. Halted: ${Boolean(payload.data.halted) ? "yes" : "no"}.`;
  }
  return payload.type ?? "stream.update";
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function mergeScopedRows<T extends { deployment_id?: string }>(
  current: T[],
  scope: string | null | undefined,
  nextRows: T[],
): T[] {
  if (!scope) {
    return nextRows;
  }
  const remainder = current.filter((row) => row.deployment_id !== scope);
  return [...remainder, ...nextRows];
}
