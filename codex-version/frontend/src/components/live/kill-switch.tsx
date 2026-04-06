"use client";

type Props = { onKillAll: () => void; disabled?: boolean };

export function KillSwitch({ onKillAll, disabled = false }: Props) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (window.confirm("Stop ALL live strategies?")) {
          onKillAll();
        }
      }}
      className="rounded-lg border border-rose-300/50 bg-rose-500/30 px-4 py-3 text-sm font-semibold text-rose-100 disabled:opacity-60"
    >
      STOP ALL
    </button>
  );
}
