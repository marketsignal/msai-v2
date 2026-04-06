type Deployment = {
  id: string;
  strategy: string;
  status: string;
  started_at?: string;
  daily_pnl?: number;
};

type Props = {
  rows: Deployment[];
  onStop: (id: string) => void;
};

export function StrategyStatus({ rows, onStop }: Props) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Active Strategies</h2>
      <div className="mt-3 space-y-3">
        {rows.map((row) => (
          <div key={row.id} className="rounded-lg border border-white/10 p-3">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium text-zinc-100">{row.strategy}</p>
                <p className="text-xs text-zinc-400">{row.id}</p>
              </div>
              <span className="rounded-full bg-cyan-500/20 px-2 py-1 text-xs text-cyan-200">{row.status}</span>
            </div>
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => onStop(row.id)}
                className="rounded border border-amber-300/40 bg-amber-500/20 px-2 py-1 text-xs text-amber-100"
              >
                Stop
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
