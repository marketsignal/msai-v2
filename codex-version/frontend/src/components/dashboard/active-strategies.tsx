type StrategyRow = {
  id: string;
  name: string;
  status: "running" | "stopped" | "error";
  dailyPnl: number;
};

export function ActiveStrategies({ items }: { items: StrategyRow[] }) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Active Strategies</h2>
      <div className="mt-4 space-y-3">
        {items.map((item) => (
          <div key={item.id} className="flex items-center justify-between rounded-lg border border-white/10 p-3">
            <div>
              <p className="font-medium text-zinc-100">{item.name}</p>
              <p className="text-xs text-zinc-400">{item.id}</p>
            </div>
            <div className="text-right">
              <span
                className={`rounded-full px-2 py-1 text-xs ${
                  item.status === "running"
                    ? "bg-emerald-500/20 text-emerald-200"
                    : item.status === "error"
                      ? "bg-rose-500/20 text-rose-200"
                      : "bg-zinc-500/20 text-zinc-300"
                }`}
              >
                {item.status}
              </span>
              <p className="mt-2 text-sm text-zinc-200">${item.dailyPnl.toFixed(2)}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
