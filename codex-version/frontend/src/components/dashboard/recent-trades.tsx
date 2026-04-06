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
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Recent Trades</h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="text-left text-zinc-400">
              <th className="py-2">Time</th>
              <th>Instrument</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>P&L</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} className="border-t border-white/10 text-zinc-200">
                <td className="py-2">{new Date(item.timestamp).toLocaleString()}</td>
                <td>{item.instrument}</td>
                <td>{item.side}</td>
                <td>{item.quantity}</td>
                <td>${item.price.toFixed(2)}</td>
                <td className={item.pnl >= 0 ? "text-emerald-300" : "text-rose-300"}>
                  ${item.pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
