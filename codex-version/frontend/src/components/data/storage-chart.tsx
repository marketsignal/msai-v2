"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type StorageRow = { asset_class: string; bytes: number };

export function StorageChart({ rows }: { rows: StorageRow[] }) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Storage by Asset Class</h2>
      <div className="mt-4 h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows}>
            <CartesianGrid stroke="rgba(255,255,255,0.08)" />
            <XAxis dataKey="asset_class" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <Tooltip />
            <Bar dataKey="bytes" fill="#22d3ee" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
