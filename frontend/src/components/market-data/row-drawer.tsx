"use client";

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import type { InventoryRow } from "@/lib/api";
import { StatusBadge } from "./status-badge";

export interface RecentJob {
  run_id: string;
  action: "onboard" | "refresh" | "repair";
  started_at: string;
  status: "succeeded" | "failed" | "in_progress";
}

interface RowDrawerProps {
  row: InventoryRow | null;
  recentJobs: RecentJob[];
  onClose: () => void;
  onRefresh: (row: InventoryRow) => void;
  onRepairRange: (
    row: InventoryRow,
    range: { start: string; end: string },
  ) => void;
  onRemove: (row: InventoryRow) => void;
  onViewChart: (row: InventoryRow) => void;
}

export function RowDrawer({
  row,
  recentJobs,
  onClose,
  onRefresh,
  onRepairRange,
  onRemove,
  onViewChart,
}: RowDrawerProps): React.ReactElement {
  return (
    <Sheet open={row !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent
        className="w-[420px] sm:max-w-[420px] overflow-y-auto"
        data-testid="row-drawer"
      >
        {row && (
          <>
            <SheetHeader className="space-y-1">
              <SheetTitle className="font-mono text-xl">
                {row.symbol}
              </SheetTitle>
              <p className="text-xs text-muted-foreground">
                {row.asset_class} · {row.provider}
              </p>
            </SheetHeader>

            <div className="mt-3">
              <StatusBadge value={row.status} className="text-sm" />
            </div>

            <Section title="Actions">
              <div className="flex gap-2 flex-wrap">
                <Button
                  size="sm"
                  onClick={() => onRefresh(row)}
                  data-testid="drawer-refresh"
                >
                  ↻ Refresh
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => onViewChart(row)}
                >
                  📈 View chart
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => onRemove(row)}
                >
                  🗑 Remove
                </Button>
              </div>
            </Section>

            <Section title="Coverage">
              <p className="text-sm text-muted-foreground mb-2">
                {row.covered_range ?? "no data"}
              </p>
              {row.missing_ranges.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">
                  No gaps in current window.
                </p>
              ) : (
                <div className="space-y-1">
                  {row.missing_ranges.map((r) => (
                    <div
                      key={`${r.start}-${r.end}`}
                      className="flex items-center justify-between rounded border border-yellow-500/30 bg-yellow-500/[0.10] px-2 py-1.5 text-xs"
                    >
                      <span>
                        Missing {r.start} → {r.end}
                      </span>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-6 text-xs"
                        onClick={() => onRepairRange(row, r)}
                        data-testid={`repair-${r.start}-${r.end}`}
                      >
                        Repair
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section title="Recent jobs">
              {recentJobs.length === 0 ? (
                <p className="text-xs text-muted-foreground italic">
                  No recent jobs.
                </p>
              ) : (
                <div className="space-y-1">
                  {recentJobs.slice(0, 5).map((j) => (
                    <div
                      key={j.run_id}
                      className="flex items-center justify-between text-xs text-muted-foreground py-1"
                    >
                      <span>
                        {j.action} ·{" "}
                        {new Date(j.started_at).toISOString().slice(0, 10)}
                      </span>
                      <span
                        className={
                          j.status === "succeeded"
                            ? "text-emerald-400"
                            : j.status === "failed"
                              ? "text-red-400"
                              : "text-sky-400"
                        }
                      >
                        {j.status === "succeeded"
                          ? "✓ done"
                          : j.status === "failed"
                            ? "✕ failed"
                            : "⏵ running"}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Section>

            <Section title="Metadata">
              <dl className="text-xs text-muted-foreground space-y-1">
                <div className="flex gap-2">
                  <dt className="w-32 shrink-0">Provider:</dt>
                  <dd>{row.provider}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-32 shrink-0">Live qualified:</dt>
                  <dd>{row.live_qualified ? "✓ yes" : "✗ no"}</dd>
                </div>
                <div className="flex gap-2">
                  <dt className="w-32 shrink-0">Last update:</dt>
                  <dd>{row.last_refresh_at ?? "—"}</dd>
                </div>
              </dl>
            </Section>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <section className="mt-4 border-t border-border/50 pt-3">
      <h3 className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-2">
        {title}
      </h3>
      {children}
    </section>
  );
}
