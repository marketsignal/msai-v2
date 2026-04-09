type StrategyRow = {
  id: string;
  name: string;
  status: "running" | "stopped" | "error" | "starting" | "liquidating" | "paused";
  dailyPnl: number;
};

export function ActiveStrategies({ items }: { items: StrategyRow[] }) {
  return (
    <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(11,16,24,0.78))] p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Active Strategies</h2>
          <p className="mt-1 text-sm text-zinc-400">Live roster with deployment state and daily contribution.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
          {items.length} loaded
        </span>
      </div>
      <div className="mt-4 space-y-3">
        {items.length === 0 ? (
          <div className="rounded-[1.2rem] border border-dashed border-white/10 bg-black/20 px-4 py-8 text-sm text-zinc-500">
            No live deployments are active yet. Promote a candidate into paper or live trading to populate this desk.
          </div>
        ) : null}
        {items.map((item) => (
          <div
            key={item.id}
            className="flex items-center justify-between gap-3 rounded-[1.2rem] border border-white/10 bg-white/[0.03] p-4"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span
                  className={`inline-flex h-2.5 w-2.5 rounded-full ${
                    item.status === "running"
                      ? "bg-emerald-400"
                      : item.status === "starting" || item.status === "liquidating"
                        ? "bg-amber-400"
                      : item.status === "error"
                        ? "bg-rose-400"
                        : "bg-zinc-500"
                  }`}
                />
                <p className="truncate font-medium text-zinc-100">{item.name}</p>
              </div>
              <p className="mt-1 text-xs text-zinc-500">{item.id}</p>
            </div>
            <div className="text-right">
              <span
                className={`rounded-full px-2.5 py-1 text-xs ${
                  item.status === "running"
                    ? "bg-emerald-500/20 text-emerald-200"
                    : item.status === "starting" || item.status === "liquidating"
                      ? "bg-amber-500/20 text-amber-100"
                    : item.status === "error"
                      ? "bg-rose-500/20 text-rose-200"
                      : "bg-zinc-500/20 text-zinc-300"
                }`}
              >
                {item.status}
              </span>
              <p className={`mt-2 text-sm ${item.dailyPnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                {item.dailyPnl >= 0 ? "+" : ""}
                ${item.dailyPnl.toFixed(2)}
              </p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
