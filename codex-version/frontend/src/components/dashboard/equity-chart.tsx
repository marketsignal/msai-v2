"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type EquityPoint = { timestamp: string; value: number };

export function EquityChart({ data }: { data: EquityPoint[] }) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Equity Curve</h2>
      <div className="mt-4 h-64">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#2dd4bf" stopOpacity={0.5} />
                <stop offset="95%" stopColor="#2dd4bf" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,0.08)" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={(value) => new Date(value).toLocaleDateString()}
              stroke="#94a3b8"
            />
            <YAxis stroke="#94a3b8" />
            <Tooltip
              labelFormatter={(value) => new Date(value).toLocaleString()}
              contentStyle={{ backgroundColor: "#0b1220", border: "1px solid rgba(255,255,255,0.2)" }}
            />
            <Area type="monotone" dataKey="value" stroke="#2dd4bf" fill="url(#equityGradient)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
