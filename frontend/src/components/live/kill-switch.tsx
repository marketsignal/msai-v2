"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { OctagonX, AlertTriangle } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { killAllLive, type LiveKillAllResponse } from "@/lib/api";
import { FlatnessDisplay } from "@/components/live/flatness-display";

interface KillSwitchProps {
  activeCount: number;
  positionCount: number;
  /**
   * Codex code-review P2: parent must refresh `/live/status` after a
   * successful kill-all so the persistent Redis halt flag surfaces
   * immediately (ResumeButton + risk-halted banner depend on
   * `data?.risk_halted`). Without this callback the operator would
   * have to manually reload to see the Resume action.
   */
  onKilled?: () => void | Promise<void>;
}

export function KillSwitch({
  activeCount,
  positionCount,
  onKilled,
}: KillSwitchProps): React.ReactElement {
  const { getToken } = useAuth();
  const [open, setOpen] = useState(false);
  const [killResult, setKillResult] = useState<LiveKillAllResponse | null>(
    null,
  );
  const [killError, setKillError] = useState<string | null>(null);

  const handleKillAll = async (): Promise<void> => {
    setKillError(null);
    try {
      const token = await getToken();
      const result = await killAllLive(token);
      setKillResult(result);
      // Refresh parent — see Codex P2 note on the prop docstring.
      await onKilled?.();
    } catch (error) {
      console.error("Kill all failed:", error);
      setKillError(
        error instanceof Error ? error.message : "Kill-all request failed",
      );
    }
    setOpen(false);
  };

  return (
    <div className="flex flex-col items-end gap-3">
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button variant="destructive" className="gap-1.5">
            <OctagonX className="size-4" />
            STOP ALL
          </Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Kill Switch - Stop All Trading</DialogTitle>
            <DialogDescription>
              This will immediately stop all running strategies, cancel all
              pending orders, and close all open positions. This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
            <p className="text-sm font-medium text-red-400">
              Are you sure you want to stop all trading activity?
            </p>
            <p className="mt-1 text-xs text-red-400/80">
              {activeCount} active deployment(s) and {positionCount} open
              position(s) will be affected.
            </p>
          </div>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleKillAll}
              className="gap-1.5"
            >
              <OctagonX className="size-4" />
              Confirm Stop All
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {killError !== null ? (
        <div
          data-testid="kill-all-error"
          role="alert"
          className="w-full max-w-xl rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300"
        >
          Kill-all failed: {killError}
        </div>
      ) : null}

      {killResult !== null ? (
        <section
          data-testid="kill-all-result-panel"
          aria-label="Kill-all result"
          className="w-full max-w-xl space-y-4 rounded-lg border border-border/60 bg-card/60 p-4"
        >
          <header className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">
              Kill-all result
            </h3>
            <p className="text-xs text-muted-foreground">
              stopped: <span className="font-mono">{killResult.stopped}</span> ·
              failed_publish:{" "}
              <span className="font-mono">{killResult.failed_publish}</span> ·
              risk_halted:{" "}
              <span className="font-mono">
                {killResult.risk_halted ? "✓" : "✗"}
              </span>
            </p>
            {killResult.any_non_flat ? (
              <div
                data-testid="kill-all-non-flat-warning"
                role="alert"
                className="flex items-start gap-2 rounded-md border border-red-500/50 bg-red-500/15 p-3 text-sm text-red-200"
              >
                <AlertTriangle
                  className="mt-0.5 size-4 shrink-0 text-red-300"
                  aria-hidden="true"
                />
                <span>
                  <strong className="font-semibold">
                    At least one deployment is not flat.
                  </strong>{" "}
                  Verify residual positions via IB portal before resuming.
                </span>
              </div>
            ) : null}
          </header>

          {killResult.flatness_reports.length > 0 ? (
            <ul className="space-y-4">
              {killResult.flatness_reports.map((report) => (
                <li
                  key={report.deployment_id}
                  className="space-y-2 rounded-md border border-border/40 bg-background/40 p-3"
                >
                  <h4 className="font-mono text-xs text-muted-foreground">
                    {report.deployment_id.slice(0, 12)}…
                  </h4>
                  <FlatnessDisplay kind="kill-all-entry" data={report} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">
              No flatness reports returned (no active deployments at kill time).
            </p>
          )}
        </section>
      ) : null}
    </div>
  );
}
