"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type EquityPoint = { timestamp: string; value: number };

export function EquityChart({ data }: { data: EquityPoint[] }) {
  return (
    <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Equity Curve</h2>
          <p className="mt-1 text-sm text-zinc-400">Capital trajectory across the current research-to-live loop.</p>
        </div>
      </div>
      <div className="mt-4 h-72">
        {data.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#2dd4bf" stopOpacity={0.55} />
                  <stop offset="65%" stopColor="#22d3ee" stopOpacity={0.18} />
                  <stop offset="100%" stopColor="#22d3ee" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(255,255,255,0.08)" vertical={false} />
              <XAxis
                dataKey="timestamp"
                tickFormatter={(value) => new Date(value).toLocaleDateString()}
                stroke="#64748b"
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                stroke="#64748b"
                tickLine={false}
                axisLine={false}
                tickFormatter={(value) => `$${Math.round(value / 1000)}k`}
              />
              <Tooltip
                labelFormatter={(value) => new Date(value).toLocaleString()}
                formatter={(value: number) => [`$${value.toLocaleString()}`, "Equity"]}
                contentStyle={{
                  backgroundColor: "#09111b",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: "16px",
                }}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke="#2dd4bf"
                strokeWidth={2.25}
                fill="url(#equityGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center rounded-[1.25rem] border border-dashed border-white/10 bg-black/20 px-6 text-center text-sm leading-6 text-zinc-500">
            No real portfolio or backtest equity series is available yet. Run a portfolio backtest or wait for live
            runtime snapshots to accumulate.
          </div>
        )}
      </div>
    </section>
  );
}
