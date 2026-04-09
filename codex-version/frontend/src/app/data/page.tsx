"use client";

import { useEffect, useMemo, useState } from "react";

import { IngestionStatus } from "@/components/data/ingestion-status";
import { StorageChart } from "@/components/data/storage-chart";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type StatusPayload = {
  last_run_at: string | null;
  storage_stats: Record<string, { bytes: number; file_count: number }>;
};

type DailyUniverseEntry = {
  asset_class: "equities" | "futures";
  symbols: string[];
  provider: string;
  dataset: string;
  schema: string;
};

type AlertRecord = {
  type: string;
  level: string;
  title: string;
  message: string;
  created_at: string;
};

export default function DataPage() {
  const { token } = useAuth();
  const [status, setStatus] = useState<StatusPayload>({ last_run_at: null, storage_stats: {} });
  const [symbols, setSymbols] = useState<Record<string, string[]>>({});
  const [dailyUniverse, setDailyUniverse] = useState<DailyUniverseEntry[]>([]);
  const [dailyUniverseText, setDailyUniverseText] = useState("[]");
  const [alerts, setAlerts] = useState<AlertRecord[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return;

    async function load() {
      try {
        const [statusPayload, symbolsPayload, universePayload, alertsPayload] = await Promise.all([
          apiFetch<StatusPayload>("/api/v1/market-data/status", token),
          apiFetch<{ symbols: Record<string, string[]> }>("/api/v1/market-data/symbols", token),
          apiFetch<{ requests: DailyUniverseEntry[] }>("/api/v1/market-data/daily-universe", token),
          apiFetch<{ alerts: AlertRecord[] }>("/api/v1/alerts/", token),
        ]);
        setStatus(statusPayload);
        setSymbols(symbolsPayload.symbols);
        setDailyUniverse(universePayload.requests);
        setDailyUniverseText(JSON.stringify(universePayload.requests, null, 2));
        setAlerts(alertsPayload.alerts);
        setError("");
      } catch (fetchError) {
        const loadMessage = fetchError instanceof Error ? fetchError.message : "Failed to load data control";
        setError(loadMessage);
      }
    }

    void load();
  }, [token]);

  const rows = useMemo(() => {
    return Object.entries(status.storage_stats).map(([asset_class, stats]) => ({
      asset_class,
      bytes: stats.bytes,
    }));
  }, [status.storage_stats]);

  return (
    <div className="space-y-4">
      {error ? (
        <div className="rounded-xl border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div>
      ) : null}
      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <StorageChart rows={rows} />
        <IngestionStatus
          lastRun={status.last_run_at}
          onTrigger={() => {
            if (!token) return;
            void (async () => {
              try {
                await apiFetch("/api/v1/market-data/ingest-daily-configured", token, {
                  method: "POST",
                });
                setStatus((current) => ({
                  ...current,
                  last_run_at: new Date().toISOString(),
                }));
                setMessage("Configured daily universe queued.");
              } catch {
                setMessage("Failed to queue the configured daily universe.");
              }
            })();
          }}
        />
      </div>

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-white">Daily Universe</h2>
            <p className="mt-2 text-sm text-zinc-300">
              This JSON is the scheduler-controlled daily refresh universe used by the API and the dedicated
              `daily-scheduler` container.
            </p>
          </div>
          {message ? <p className="text-sm text-emerald-200">{message}</p> : null}
        </div>
        <textarea
          value={dailyUniverseText}
          onChange={(event) => setDailyUniverseText(event.target.value)}
          rows={14}
          className="mt-4 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2 font-mono text-xs text-white"
        />
        <div className="mt-4 flex items-center justify-between gap-3">
          <p className="text-xs text-zinc-500">{dailyUniverse.length} request groups currently configured.</p>
          <button
            type="button"
            onClick={() => {
              if (!token) return;
              void (async () => {
                try {
                  const parsed = JSON.parse(dailyUniverseText) as DailyUniverseEntry[];
                  const response = await apiFetch<{ requests: DailyUniverseEntry[] }>(
                    "/api/v1/market-data/daily-universe",
                    token,
                    {
                      method: "PUT",
                      body: JSON.stringify({ requests: parsed }),
                    },
                  );
                  setDailyUniverse(response.requests);
                  setDailyUniverseText(JSON.stringify(response.requests, null, 2));
                  setMessage("Daily universe saved.");
                } catch {
                  setMessage("Daily universe must be valid JSON before it can be saved.");
                }
              })();
            }}
            className="rounded-md border border-cyan-300/40 bg-cyan-500/20 px-3 py-2 text-sm text-cyan-100"
          >
            Save Daily Universe
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h2 className="text-lg font-semibold text-white">Available Symbols</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Object.keys(symbols).length === 0 ? <p className="text-sm text-zinc-500">No symbols have been indexed yet.</p> : null}
          {Object.entries(symbols).map(([asset, list]) => (
            <article key={asset} className="rounded-lg border border-white/10 p-3">
              <h3 className="text-sm font-semibold text-zinc-200">{asset}</h3>
              <p className="mt-2 text-sm text-zinc-400">{list.join(", ") || "None"}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h2 className="text-lg font-semibold text-white">Recent Alerts</h2>
        <div className="mt-3 space-y-3">
          {alerts.length === 0 ? <p className="text-sm text-zinc-400">No alerts recorded.</p> : null}
          {alerts.slice(0, 10).map((alert) => (
            <article key={`${alert.created_at}-${alert.title}`} className="rounded-lg border border-white/10 p-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-white">{alert.title}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.2em] text-zinc-500">
                    {alert.level} · {alert.type}
                  </p>
                </div>
                <p className="text-xs text-zinc-500">{new Date(alert.created_at).toLocaleString()}</p>
              </div>
              <p className="mt-2 text-sm text-zinc-300">{alert.message}</p>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
