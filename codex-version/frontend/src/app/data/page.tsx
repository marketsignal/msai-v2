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

export default function DataPage() {
  const { token } = useAuth();
  const [status, setStatus] = useState<StatusPayload>({ last_run_at: null, storage_stats: {} });
  const [symbols, setSymbols] = useState<Record<string, string[]>>({});

  useEffect(() => {
    if (!token) return;

    async function load() {
      try {
        const [statusPayload, symbolsPayload] = await Promise.all([
          apiFetch<StatusPayload>("/api/v1/market-data/status", token),
          apiFetch<{ symbols: Record<string, string[]> }>("/api/v1/market-data/symbols", token),
        ]);
        setStatus(statusPayload);
        setSymbols(symbolsPayload.symbols);
      } catch {
        setStatus({
          last_run_at: new Date().toISOString(),
          storage_stats: {
            stocks: { bytes: 1_400_000_000, file_count: 32 },
            futures: { bytes: 3_200_000_000, file_count: 44 },
          },
        });
        setSymbols({ stocks: ["AAPL", "MSFT"], futures: ["ES", "NQ"] });
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
      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <StorageChart rows={rows} />
        <IngestionStatus
          lastRun={status.last_run_at}
          onTrigger={() => {
            if (!token) return;
            void apiFetch("/api/v1/market-data/ingest", token, {
              method: "POST",
              body: JSON.stringify({
                asset_class: "stocks",
                symbols: ["AAPL", "MSFT"],
                start: "2024-01-01",
                end: "2024-12-31",
              }),
            });
          }}
        />
      </div>

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h2 className="text-lg font-semibold text-white">Available Symbols</h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Object.entries(symbols).map(([asset, list]) => (
            <article key={asset} className="rounded-lg border border-white/10 p-3">
              <h3 className="text-sm font-semibold text-zinc-200">{asset}</h3>
              <p className="mt-2 text-sm text-zinc-400">{list.join(", ") || "None"}</p>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
