type PortfolioSummaryProps = {
  totalValue: number;
  dailyPnl: number;
  totalReturn: number;
  activeStrategies: number;
};

function asMoney(value: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);
}

export function PortfolioSummary({
  totalValue,
  dailyPnl,
  totalReturn,
  activeStrategies,
}: PortfolioSummaryProps) {
  const cards = [
    { label: "Net Liquidation", value: asMoney(totalValue) },
    {
      label: "Daily P&L",
      value: asMoney(dailyPnl),
      tone: dailyPnl >= 0 ? "text-emerald-300" : "text-rose-300",
    },
    { label: "Total Return", value: `${(totalReturn * 100).toFixed(2)}%` },
    { label: "Active Strategies", value: String(activeStrategies) },
  ];

  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => (
        <article key={card.label} className="rounded-xl border border-white/10 bg-white/5 p-4 backdrop-blur">
          <p className="text-xs uppercase tracking-[0.18em] text-zinc-400">{card.label}</p>
          <p className={`mt-2 text-2xl font-semibold text-white ${card.tone ?? ""}`}>{card.value}</p>
        </article>
      ))}
    </section>
  );
}
