"use client";

import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useJobStatusQuery } from "@/lib/hooks/use-job-status-query";

interface JobsDrawerProps {
  open: boolean;
  activeRunIds: string[];
  onClose: () => void;
}

export function JobsDrawer({
  open,
  activeRunIds,
  onClose,
}: JobsDrawerProps): React.ReactElement {
  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        className="w-[420px] sm:max-w-[420px]"
        data-testid="jobs-drawer"
      >
        <SheetHeader>
          <SheetTitle>Jobs</SheetTitle>
        </SheetHeader>

        <section className="mt-4 border-t border-border/50 pt-3">
          <h3 className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground mb-2">
            Active
          </h3>
          {activeRunIds.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No active jobs.
            </p>
          ) : (
            <div className="space-y-2">
              {activeRunIds.map((runId) => (
                <JobRow key={runId} runId={runId} />
              ))}
            </div>
          )}
        </section>
      </SheetContent>
    </Sheet>
  );
}

function JobRow({ runId }: { runId: string }): React.ReactElement {
  const { data } = useJobStatusQuery(runId);
  if (!data) return <p className="text-xs text-muted-foreground">Loading…</p>;
  const { progress, status, watchlist_name } = data;
  return (
    <div className="rounded border border-border/50 bg-secondary/40 p-2">
      <div className="flex items-center justify-between text-xs">
        <span className="font-mono">{watchlist_name}</span>
        <span className="text-muted-foreground">{status}</span>
      </div>
      <div className="mt-1 text-xs text-muted-foreground">
        {progress.succeeded}/{progress.total} succeeded · {progress.failed}{" "}
        failed
      </div>
    </div>
  );
}
