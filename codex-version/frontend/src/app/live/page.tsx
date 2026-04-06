"use client";

import { useCallback, useEffect, useState } from "react";

import { KillSwitch } from "@/components/live/kill-switch";
import { PositionsTable } from "@/components/live/positions-table";
import { StrategyStatus } from "@/components/live/strategy-status";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type Deployment = { id: string; strategy: string; status: string; started_at?: string; daily_pnl?: number };
type Position = {
  instrument: string;
  quantity: number;
  avg_price: number;
  current_price?: number;
  unrealized_pnl: number;
  market_value: number;
};
type StrategySummary = { id: string; name: string };
type StrategyDetail = { default_config?: Record<string, unknown> };

export default function LivePage() {
  const { token } = useAuth();
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [instrumentsInput, setInstrumentsInput] = useState("AAPL");
  const [configText, setConfigText] = useState("{}");
  const [startError, setStartError] = useState("");
  const [starting, setStarting] = useState(false);
  const [connected, setConnected] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const [status, positionRows, strategyRows] = await Promise.all([
        apiFetch<Deployment[]>("/api/v1/live/status", token),
        apiFetch<Position[]>("/api/v1/live/positions", token),
        apiFetch<StrategySummary[]>("/api/v1/strategies/", token),
      ]);
      setDeployments(status);
      setPositions(positionRows);
      setStrategies(strategyRows);
      if (
        strategyRows[0]?.id &&
        (!selectedStrategyId || !strategyRows.some((strategy) => strategy.id === selectedStrategyId))
      ) {
        setSelectedStrategyId(strategyRows[0].id);
      }
    } catch {
      setDeployments([{ id: "dep-1", strategy: "EMA Cross", status: "running", daily_pnl: 122.3 }]);
      setPositions([
        {
          instrument: "AAPL",
          quantity: 25,
          avg_price: 212.4,
          current_price: 214.2,
          unrealized_pnl: 45,
          market_value: 5355,
        },
      ]);
      setStrategies([{ id: "demo-ema", name: "example.ema_cross" }]);
      if (!selectedStrategyId) {
        setSelectedStrategyId("demo-ema");
      }
    }
  }, [selectedStrategyId, token]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!token || !selectedStrategyId) return;

    async function loadDefaultConfig() {
      try {
        const detail = await apiFetch<StrategyDetail>(`/api/v1/strategies/${selectedStrategyId}`, token);
        setConfigText(JSON.stringify(detail.default_config ?? {}, null, 2));
      } catch {
        setConfigText("{}");
      }
    }

    void loadDefaultConfig();
  }, [selectedStrategyId, token]);

  useEffect(() => {
    if (!token) return;

    const ws = new WebSocket(`${(process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace("http", "ws")}/api/v1/live/stream`);

    ws.addEventListener("open", () => {
      setConnected(true);
      ws.send(token);
    });

    ws.addEventListener("close", () => {
      setConnected(false);
    });

    return () => {
      ws.close();
    };
  }, [token]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between rounded-lg border border-white/10 bg-black/25 p-4">
        <p className="text-sm text-zinc-300">
          Stream status: <span className={connected ? "text-emerald-300" : "text-amber-300"}>{connected ? "connected" : "reconnecting"}</span>
        </p>
        <KillSwitch
          onKillAll={() => {
            if (!token) return;
            void apiFetch<{ stopped: number }>("/api/v1/live/kill-all", token, { method: "POST" }).then(() => load());
          }}
        />
      </div>

      <form
        className="space-y-3 rounded-xl border border-white/10 bg-black/25 p-4"
        onSubmit={(event) => {
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

          setStarting(true);
          setStartError("");
          void apiFetch<{ deployment_id: string }>("/api/v1/live/start", token, {
            method: "POST",
            body: JSON.stringify({
              strategy_id: selectedStrategyId,
              config,
              instruments,
              paper_trading: true,
            }),
          })
            .then(() => load())
            .catch((err: unknown) => {
              const message = err instanceof Error ? err.message : "Start failed";
              setStartError(message);
            })
            .finally(() => setStarting(false));
        }}
      >
        <h2 className="text-lg font-semibold text-white">Deploy Strategy</h2>
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1 text-sm text-zinc-300">
            Strategy
            <select
              className="w-full rounded-md border border-white/10 bg-black/40 p-2"
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
          <label className="space-y-1 text-sm text-zinc-300">
            Instruments
            <input
              className="w-full rounded-md border border-white/10 bg-black/40 p-2"
              value={instrumentsInput}
              onChange={(event) => setInstrumentsInput(event.target.value)}
              placeholder="AAPL,MSFT"
            />
          </label>
        </div>
        <label className="space-y-1 text-sm text-zinc-300">
          Config JSON
          <textarea
            className="min-h-24 w-full rounded-md border border-white/10 bg-black/40 p-2 font-mono text-xs"
            value={configText}
            onChange={(event) => setConfigText(event.target.value)}
          />
        </label>
        {startError ? <p className="text-sm text-rose-300">{startError}</p> : null}
        <button
          type="submit"
          disabled={starting || !selectedStrategyId}
          className="rounded border border-emerald-300/40 bg-emerald-500/20 px-3 py-2 text-sm text-emerald-100 disabled:opacity-60"
        >
          {starting ? "Starting..." : "Start"}
        </button>
      </form>

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

      <PositionsTable rows={positions} />
    </div>
  );
}
