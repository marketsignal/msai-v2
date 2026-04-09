"use client";

import { useEffect, useState } from "react";

type StrategyOption = { id: string; name: string };

type RunFormProps = {
  strategies: StrategyOption[];
  onRun: (payload: {
    strategy_id: string;
    instruments: string[];
    start_date: string;
    end_date: string;
    config: Record<string, unknown>;
  }) => Promise<void>;
};

export function RunForm({ strategies, onRun }: RunFormProps) {
  const [strategyId, setStrategyId] = useState<string>(strategies[0]?.id ?? "");
  const [instruments, setInstruments] = useState("AAPL.EQUS");
  const [startDate, setStartDate] = useState("2024-01-01");
  const [endDate, setEndDate] = useState("2024-12-31");
  const [configText, setConfigText] = useState("{}");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!strategyId && strategies[0]?.id) {
      setStrategyId(strategies[0].id);
    }
  }, [strategies, strategyId]);

  return (
    <form
      className="space-y-4 rounded-xl border border-white/10 bg-black/25 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        let config: Record<string, unknown> = {};
        try {
          config = JSON.parse(configText) as Record<string, unknown>;
        } catch {
          return;
        }

        setRunning(true);
        if (!strategyId) {
          setRunning(false);
          return;
        }
        void onRun({
          strategy_id: strategyId,
          instruments: instruments.split(",").map((item) => item.trim()).filter(Boolean),
          start_date: startDate,
          end_date: endDate,
          config,
        }).finally(() => setRunning(false));
      }}
    >
      <div className="grid gap-3 md:grid-cols-2">
        <label className="space-y-1 text-sm text-zinc-300">
          Strategy
          <select
            className="w-full rounded-md border border-white/10 bg-black/40 p-2"
            value={strategyId}
            onChange={(event) => setStrategyId(event.target.value)}
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
            value={instruments}
            onChange={(event) => setInstruments(event.target.value)}
            placeholder="AAPL.EQUS,MSFT.EQUS"
          />
        </label>
        <label className="space-y-1 text-sm text-zinc-300">
          Start Date
          <input
            className="w-full rounded-md border border-white/10 bg-black/40 p-2"
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm text-zinc-300">
          End Date
          <input
            className="w-full rounded-md border border-white/10 bg-black/40 p-2"
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
          />
        </label>
      </div>
      <label className="space-y-1 text-sm text-zinc-300">
        Config JSON
        <textarea
          className="min-h-28 w-full rounded-md border border-white/10 bg-black/40 p-2 font-mono text-xs"
          value={configText}
          onChange={(event) => setConfigText(event.target.value)}
        />
      </label>
      <button
        type="submit"
        disabled={running || !strategyId}
        className="rounded-md border border-cyan-300/40 bg-cyan-500/20 px-4 py-2 text-sm text-cyan-100 disabled:opacity-60"
      >
        {running ? "Submitting..." : "Run Backtest"}
      </button>
    </form>
  );
}
