"use client";

type TradeRow = {
  timestamp: string;
  instrument: string;
  side: string;
  quantity: number;
  price: number;
  pnl: number;
};

export function TradeLog({ rows }: { rows: TradeRow[] }) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Trade Log</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="text-left text-zinc-400">
              <th className="py-2">Timestamp</th>
              <th>Instrument</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>P&L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={`${row.timestamp}-${idx}`} className="border-t border-white/10 text-zinc-200">
                <td className="py-2">{new Date(row.timestamp).toLocaleString()}</td>
                <td>{row.instrument}</td>
                <td>{row.side}</td>
                <td>{row.quantity}</td>
                <td>{row.price.toFixed(2)}</td>
                <td className={row.pnl >= 0 ? "text-emerald-300" : "text-rose-300"}>
                  {row.pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
