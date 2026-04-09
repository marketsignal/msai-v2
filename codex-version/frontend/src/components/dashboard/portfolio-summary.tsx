type PortfolioSummaryProps = {
  totalValue: number;
  dailyPnl: number;
  totalReturn: number;
  activeStrategies: number;
};

function asMoney(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

export function PortfolioSummary({
  totalValue,
  dailyPnl,
  totalReturn,
  activeStrategies,
}: PortfolioSummaryProps) {
  const cards = [
    {
      label: "Net Liquidation",
      value: asMoney(totalValue),
      note: "Firmwide capital snapshot",
      tone: "text-white",
    },
    {
      label: "Daily P&L",
      value: asMoney(dailyPnl),
      note: "Intraday realized + unrealized",
      tone: dailyPnl >= 0 ? "text-emerald-300" : "text-rose-300",
    },
    {
      label: "Return",
      value: `${(totalReturn * 100).toFixed(2)}%`,
      note: "Current session efficiency",
      tone: totalReturn >= 0 ? "text-cyan-100" : "text-amber-200",
    },
    {
      label: "Active Strategies",
      value: String(activeStrategies),
      note: "Currently deployed models",
      tone: "text-violet-100",
    },
  ];

  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map((card) => (
        <article
          key={card.label}
          className="rounded-[1.35rem] border border-white/10 bg-[linear-gradient(180deg,rgba(255,255,255,0.07),rgba(255,255,255,0.03))] p-4 shadow-[0_16px_60px_rgba(0,0,0,0.22)] backdrop-blur"
        >
          <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">{card.label}</p>
          <p className={`mt-3 text-3xl font-semibold ${card.tone}`}>{card.value}</p>
          <p className="mt-2 text-sm text-zinc-400">{card.note}</p>
        </article>
      ))}
    </section>
  );
}
