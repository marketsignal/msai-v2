"use client";

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

type Point = { timestamp: string; equity: number; drawdown: number };

type Props = {
  metrics: Record<string, number>;
  series: Point[];
};

export function ResultsCharts({ metrics, series }: Props) {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {Object.entries(metrics).map(([key, value]) => (
          <article key={key} className="rounded-lg border border-white/10 bg-black/25 p-3">
            <p className="text-xs uppercase tracking-[0.16em] text-zinc-400">{key}</p>
            <p className="mt-2 text-2xl font-semibold text-zinc-100">{value.toFixed(3)}</p>
          </article>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="rounded-xl border border-white/10 bg-black/25 p-4">
          <h3 className="text-lg font-semibold text-white">Equity Curve</h3>
          <div className="mt-4 h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series}>
                <CartesianGrid stroke="rgba(255,255,255,0.08)" />
                <XAxis dataKey="timestamp" hide />
                <YAxis stroke="#94a3b8" />
                <Tooltip />
                <Line type="monotone" dataKey="equity" stroke="#22d3ee" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="rounded-xl border border-white/10 bg-black/25 p-4">
          <h3 className="text-lg font-semibold text-white">Drawdown</h3>
          <div className="mt-4 h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series}>
                <CartesianGrid stroke="rgba(255,255,255,0.08)" />
                <XAxis dataKey="timestamp" hide />
                <YAxis stroke="#94a3b8" />
                <Tooltip />
                <Area type="monotone" dataKey="drawdown" stroke="#f43f5e" fill="#f43f5e33" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>
    </div>
  );
}
