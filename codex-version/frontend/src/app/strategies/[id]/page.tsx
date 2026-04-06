"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { ConfigEditor } from "@/components/strategies/config-editor";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type StrategyDetail = {
  id: string;
  name: string;
  description?: string | null;
  default_config?: Record<string, unknown>;
};

export default function StrategyDetailPage() {
  const params = useParams<{ id: string }>();
  const { token } = useAuth();
  const [detail, setDetail] = useState<StrategyDetail | null>(null);
  const [config, setConfig] = useState<Record<string, unknown>>({});

  useEffect(() => {
    if (!token || !params.id) return;

    async function load() {
      try {
        const payload = await apiFetch<StrategyDetail>(`/api/v1/strategies/${params.id}`, token);
        setDetail(payload);
        setConfig(payload.default_config ?? {});
      } catch {
        setDetail({ id: params.id, name: "example.ema_cross", description: "Fallback strategy" });
        setConfig({ fast_ema_period: 10, slow_ema_period: 30, trade_size: 1 });
      }
    }

    void load();
  }, [params.id, token]);

  if (!detail) {
    return <div className="text-zinc-300">Loading strategy...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.18em] text-zinc-400">Strategy</p>
          <h2 className="text-2xl font-semibold text-white">{detail.name}</h2>
        </div>
        <div className="flex gap-2">
          <button className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-zinc-100" type="button">
            Validate
          </button>
          <Link
            href={`/backtests?strategy=${detail.id}`}
            className="rounded-md border border-cyan-300/40 bg-cyan-500/20 px-3 py-1.5 text-sm text-cyan-100"
          >
            Run Backtest
          </Link>
        </div>
      </div>
      <p className="text-sm text-zinc-300">{detail.description}</p>
      <ConfigEditor value={config} onChange={setConfig} />
    </div>
  );
}
