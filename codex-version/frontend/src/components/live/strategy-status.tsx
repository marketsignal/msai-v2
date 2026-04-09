type Deployment = {
  id: string;
  strategy: string;
  status: string;
  started_at?: string;
  daily_pnl?: number;
  open_positions?: number;
  open_orders?: number;
  updated_at?: string;
};

type Props = {
  rows: Deployment[];
  onStop: (id: string) => void;
};

export function StrategyStatus({ rows, onStop }: Props) {
  return (
    <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Active Strategies</h2>
          <p className="mt-1 text-sm text-zinc-400">Deployment state, open risk, and graceful stop controls.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
          {rows.length} deployments
        </span>
      </div>
      <div className="mt-4 space-y-3">
        {rows.map((row) => (
          <div key={row.id} className="rounded-[1.2rem] border border-white/10 bg-white/[0.03] p-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`inline-flex h-2.5 w-2.5 rounded-full ${statusDot(row.status)}`} />
                  <p className="truncate font-medium text-zinc-100">{row.strategy}</p>
                </div>
                <p className="mt-1 text-xs text-zinc-500">{row.id}</p>
                <p className="mt-2 text-xs text-zinc-400">
                  Started {row.started_at ? new Date(row.started_at).toLocaleString() : "pending"}
                </p>
              </div>

              <div className="grid gap-3 sm:grid-cols-3 lg:min-w-[360px]">
                <LiveMetric label="Status" value={row.status} />
                <LiveMetric label="Open Risk" value={`${row.open_positions ?? 0} pos / ${row.open_orders ?? 0} ord`} />
                <LiveMetric
                  label="Daily P&L"
                  value={`${row.daily_pnl && row.daily_pnl > 0 ? "+" : ""}${(row.daily_pnl ?? 0).toFixed(2)}`}
                  positive={(row.daily_pnl ?? 0) >= 0}
                />
              </div>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs text-zinc-500">
                Last update {row.updated_at ? new Date(row.updated_at).toLocaleString() : "pending snapshot"}
              </p>
              <button
                type="button"
                onClick={() => onStop(row.id)}
                className="rounded-2xl border border-amber-300/40 bg-amber-500/15 px-3 py-2 text-xs font-medium text-amber-100"
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

function LiveMetric({
  label,
  value,
  positive = true,
}: {
  label: string;
  value: string;
  positive?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-black/20 px-3 py-3">
      <p className="text-[10px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className={`mt-2 text-sm font-semibold ${positive ? "text-white" : "text-rose-300"}`}>{value}</p>
    </div>
  );
}

function statusDot(status: string): string {
  if (status === "running") return "bg-emerald-400";
  if (status === "starting" || status === "liquidating") return "bg-amber-400";
  if (status === "error" || status === "stale" || status === "reconcile_required") return "bg-rose-400";
  return "bg-zinc-500";
}
