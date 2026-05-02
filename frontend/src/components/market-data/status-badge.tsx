import { cn } from "@/lib/utils";
import type { InventoryStatus } from "@/lib/api";

const VARIANTS: Record<
  InventoryStatus,
  { label: string; bg: string; fg: string; icon: string }
> = {
  ready: {
    label: "Ready",
    bg: "bg-emerald-500/15",
    fg: "text-emerald-400",
    icon: "●",
  },
  stale: {
    label: "Stale",
    bg: "bg-yellow-500/15",
    fg: "text-yellow-400",
    icon: "⚠",
  },
  gapped: {
    label: "Gapped",
    bg: "bg-orange-500/15",
    fg: "text-orange-400",
    icon: "⚠",
  },
  backtest_only: {
    label: "Backtest only",
    bg: "bg-sky-500/15",
    fg: "text-sky-400",
    icon: "📊",
  },
  live_only: {
    label: "Live only",
    bg: "bg-violet-500/15",
    fg: "text-violet-400",
    icon: "📡",
  },
  not_registered: {
    label: "Not registered",
    bg: "bg-zinc-500/15",
    fg: "text-zinc-400",
    icon: "○",
  },
};

interface StatusBadgeProps {
  value: InventoryStatus;
  className?: string;
}

export function StatusBadge({
  value,
  className,
}: StatusBadgeProps): React.ReactElement {
  const v = VARIANTS[value];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        v.bg,
        v.fg,
        className,
      )}
      role="status"
      aria-label={v.label}
    >
      <span aria-hidden>{v.icon}</span>
      <span>{v.label}</span>
    </span>
  );
}
