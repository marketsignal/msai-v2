type Position = {
  instrument: string;
  quantity: number;
  avg_price: number;
  current_price?: number;
  unrealized_pnl: number;
  market_value: number;
  deployment_id?: string;
};

export function PositionsTable({ rows }: { rows: Position[] }) {
  return (
    <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Open Positions</h2>
          <p className="mt-1 text-sm text-zinc-400">Live exposures streamed from the running Nautilus node.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
          {rows.length} rows
        </span>
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[820px] text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
              <th className="pb-3">Instrument</th>
              <th className="pb-3">Deployment</th>
              <th className="pb-3">Qty</th>
              <th className="pb-3">Avg</th>
              <th className="pb-3">Current</th>
              <th className="pb-3">Unrealized</th>
              <th className="pb-3 text-right">Market Value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const currentPrice =
                row.current_price ??
                (row.quantity !== 0 ? row.market_value / row.quantity : row.avg_price);
              return (
                <tr key={`${row.instrument}-${row.deployment_id ?? "n/a"}`} className="border-t border-white/10 text-zinc-200">
                  <td className="py-3 font-medium text-white">{row.instrument}</td>
                  <td className="py-3 text-zinc-400">{row.deployment_id ?? "shared"}</td>
                  <td className="py-3">{row.quantity}</td>
                  <td className="py-3">${row.avg_price.toFixed(2)}</td>
                  <td className="py-3">${currentPrice.toFixed(2)}</td>
                  <td className={`py-3 ${row.unrealized_pnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {row.unrealized_pnl >= 0 ? "+" : ""}${row.unrealized_pnl.toFixed(2)}
                  </td>
                  <td className="py-3 text-right">${row.market_value.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
