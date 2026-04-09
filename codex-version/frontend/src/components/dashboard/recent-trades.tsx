type TradeRow = {
  id: string;
  timestamp: string;
  instrument: string;
  side: string;
  quantity: number;
  price: number;
  pnl: number;
};

export function RecentTrades({ items }: { items: TradeRow[] }) {
  return (
    <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.94),rgba(8,12,18,0.78))] p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Recent Trades</h2>
          <p className="mt-1 text-sm text-zinc-400">Execution tape across paper and live allocations.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
          {items.length} fills
        </span>
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[680px] text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-[0.22em] text-zinc-500">
              <th className="pb-3">Time</th>
              <th className="pb-3">Instrument</th>
              <th className="pb-3">Side</th>
              <th className="pb-3">Qty</th>
              <th className="pb-3">Price</th>
              <th className="pb-3 text-right">P&L</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} className="border-t border-white/10 text-zinc-200">
                <td className="py-3">{new Date(item.timestamp).toLocaleString()}</td>
                <td className="font-medium text-white">{item.instrument}</td>
                <td>
                  <span
                    className={`rounded-full px-2.5 py-1 text-xs ${
                      item.side.toUpperCase() === "BUY"
                        ? "bg-emerald-500/20 text-emerald-200"
                        : "bg-rose-500/20 text-rose-200"
                    }`}
                  >
                    {item.side}
                  </span>
                </td>
                <td>{item.quantity}</td>
                <td>${item.price.toFixed(2)}</td>
                <td className={`text-right ${item.pnl >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                  {item.pnl >= 0 ? "+" : ""}${item.pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
