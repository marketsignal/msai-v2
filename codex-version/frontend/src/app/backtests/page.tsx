"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { RunForm } from "@/components/backtests/run-form";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type Strategy = { id: string; name: string };
type Job = { id: string; status: string; created_at: string };

export default function BacktestsPage() {
  const { token } = useAuth();
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);

  useEffect(() => {
    if (!token) return;

    async function load() {
      try {
        const [strategyList, history] = await Promise.all([
          apiFetch<Strategy[]>("/api/v1/strategies/", token),
          apiFetch<Job[]>("/api/v1/backtests/history", token),
        ]);
        setStrategies(strategyList);
        setJobs(history);
      } catch {
        setStrategies([{ id: "demo-ema", name: "example.ema_cross" }]);
      }
    }

    void load();
  }, [token]);

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-semibold text-white">Backtest Runner</h2>
      <RunForm
        strategies={strategies}
        onRun={async (payload) => {
          if (!token) return;
          const run = await apiFetch<{ job_id: string }>("/api/v1/backtests/run", token, {
            method: "POST",
            body: JSON.stringify(payload),
          });
          window.location.href = `/backtests/${run.job_id}`;
        }}
      />

      <section className="rounded-xl border border-white/10 bg-black/25 p-4">
        <h3 className="text-lg font-semibold text-white">Recent Backtests</h3>
        <div className="mt-3 space-y-2">
          {jobs.map((job) => (
            <Link
              key={job.id}
              href={`/backtests/${job.id}`}
              className="flex items-center justify-between rounded-md border border-white/10 px-3 py-2 text-sm text-zinc-200 hover:bg-white/5"
            >
              <span>{job.id}</span>
              <span className="text-zinc-400">{job.status}</span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
