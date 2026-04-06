type Props = {
  lastRun: string | null;
  onTrigger: () => void;
};

export function IngestionStatus({ lastRun, onTrigger }: Props) {
  return (
    <section className="rounded-xl border border-white/10 bg-black/25 p-4">
      <h2 className="text-lg font-semibold text-white">Ingestion Status</h2>
      <p className="mt-2 text-sm text-zinc-300">Last run: {lastRun ? new Date(lastRun).toLocaleString() : "Never"}</p>
      <button
        onClick={onTrigger}
        type="button"
        className="mt-4 rounded-md border border-cyan-300/40 bg-cyan-500/20 px-3 py-2 text-sm text-cyan-100"
      >
        Trigger Download
      </button>
    </section>
  );
}
