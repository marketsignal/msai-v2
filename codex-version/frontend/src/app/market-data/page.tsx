"use client";

import { useEffect, useMemo, useState } from "react";
import type { CandlestickData, UTCTimestamp } from "lightweight-charts";

import { CandlestickChart } from "@/components/charts/candlestick-chart";
import { SymbolSelector } from "@/components/charts/symbol-selector";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type BarsResponse = {
  symbol: string;
  bars: Array<{ timestamp: string; open: number; high: number; low: number; close: number }>;
};

export default function MarketDataPage() {
  const { token } = useAuth();
  const [symbols, setSymbols] = useState<Record<string, string[]>>({ stocks: ["AAPL"] });
  const [selected, setSelected] = useState("AAPL");
  const [bars, setBars] = useState<BarsResponse["bars"]>([]);

  useEffect(() => {
    if (!token) return;
    async function loadSymbols() {
      try {
        const payload = await apiFetch<{ symbols: Record<string, string[]> }>("/api/v1/market-data/symbols", token);
        setSymbols(payload.symbols);
        const first = Object.values(payload.symbols).flat()[0];
        if (first) {
          setSelected(first);
        }
      } catch {
        setSymbols({ stocks: ["AAPL", "MSFT"], crypto: ["BTC"] });
      }
    }
    void loadSymbols();
  }, [token]);

  useEffect(() => {
    if (!token || !selected) return;

    async function loadBars() {
      const end = new Date().toISOString();
      const start = new Date(Date.now() - 30 * 24 * 3600_000).toISOString();
      try {
        const response = await apiFetch<BarsResponse>(
          `/api/v1/market-data/bars/${selected}?start=${start}&end=${end}&interval=1m`,
          token,
        );
        setBars(response.bars);
      } catch {
        setBars(
          Array.from({ length: 120 }).map((_, idx) => {
            const base = 180 + Math.sin(idx / 5) * 8;
            return {
              timestamp: new Date(Date.now() - (119 - idx) * 3600_000).toISOString(),
              open: base,
              high: base + 2,
              low: base - 2,
              close: base + Math.cos(idx / 6) * 1.5,
            };
          }),
        );
      }
    }
    void loadBars();
  }, [selected, token]);

  const chartData = useMemo<CandlestickData[]>(() => {
    return bars.map((bar) => ({
      time: Math.floor(new Date(bar.timestamp).getTime() / 1000) as UTCTimestamp,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
  }, [bars]);

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-[320px_1fr]">
        <SymbolSelector symbols={symbols} value={selected} onChange={setSelected} />
        <div className="flex items-end gap-2 text-xs text-zinc-400">
          {["1D", "1W", "1M", "3M", "1Y", "ALL"].map((label) => (
            <button key={label} type="button" className="rounded-md border border-white/10 px-2 py-1 hover:bg-white/10">
              {label}
            </button>
          ))}
        </div>
      </div>
      <CandlestickChart data={chartData} />
    </div>
  );
}
