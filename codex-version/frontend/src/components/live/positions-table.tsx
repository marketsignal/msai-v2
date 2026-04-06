type Position = {
  instrument: string;
  quantity: number;
  avg_price: number;
  current_price?: number;
  unrealized_pnl: number;
  market_value: number;
};

export function PositionsTable({ rows }: { rows: Position[] }) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Open Positions</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="text-left text-zinc-400">
              <th className="py-2">Instrument</th>
              <th>Qty</th>
              <th>Avg</th>
              <th>Current</th>
              <th>Unrealized</th>
              <th>Market Value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const currentPrice =
                row.current_price ??
                (row.quantity !== 0 ? row.market_value / row.quantity : row.avg_price);
              return (
                <tr key={row.instrument} className="border-t border-white/10 text-zinc-200">
                  <td className="py-2">{row.instrument}</td>
                  <td>{row.quantity}</td>
                  <td>{row.avg_price.toFixed(2)}</td>
                  <td>{currentPrice.toFixed(2)}</td>
                  <td className={row.unrealized_pnl >= 0 ? "text-emerald-300" : "text-rose-300"}>
                    {row.unrealized_pnl.toFixed(2)}
                  </td>
                  <td>{row.market_value.toFixed(2)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
