import Link from "next/link";

type StrategyCardProps = {
  name: string;
  description?: string | null;
  sharpe?: number;
  status?: string;
};

export function StrategyCard({ name, description, sharpe, status }: StrategyCardProps) {
  return (
    <article className="rounded-xl border border-white/10 bg-black/25 p-4 transition hover:border-cyan-300/30">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-white">{name}</h3>
          <p className="mt-1 text-sm text-zinc-400">{description ?? "No description."}</p>
        </div>
        <span className="rounded-full bg-cyan-500/20 px-2 py-1 text-xs text-cyan-200">{status ?? "ready"}</span>
      </div>
      <p className="mt-4 text-sm text-zinc-300">Last Sharpe: {sharpe?.toFixed(2) ?? "N/A"}</p>
      <Link href="/research" className="mt-4 inline-flex rounded-md border border-white/20 px-3 py-1.5 text-sm text-zinc-100 hover:bg-white/10">
        Use in Research
      </Link>
    </article>
  );
}
