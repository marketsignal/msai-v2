"use client";

import * as React from "react";
import { Play } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/lib/auth";
import { describeApiError, resumeLive } from "@/lib/api";

interface ResumeButtonProps {
  riskHalted: boolean;
  /** Invoked after `POST /api/v1/live/resume` resolves successfully. */
  onResumed?: () => void;
}

/**
 * Warning-styled button that clears the persistent risk-halt flag set by
 * kill-all. Renders `null` when `riskHalted === false` — the button has no
 * other states. A shadcn AlertDialog confirms the action before firing
 * because resume re-enables trading after an emergency halt.
 */
export function ResumeButton({
  riskHalted,
  onResumed,
}: ResumeButtonProps): React.ReactElement | null {
  const { getToken } = useAuth();
  const [submitting, setSubmitting] = React.useState<boolean>(false);

  if (!riskHalted) {
    return null;
  }

  const handleConfirm = async (): Promise<void> => {
    setSubmitting(true);
    try {
      const token = await getToken();
      await resumeLive(token);
      toast.success("Live trading resumed");
      onResumed?.();
    } catch (error) {
      // iter-3 describeApiError sweep: backend may return 409 (already
      // resumed) or 503 (Redis unreachable) — surface detail.
      toast.error(describeApiError(error, "Failed to resume trading"));
      console.error("Resume live failed:", error);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button
          data-testid="resume-button"
          type="button"
          className="gap-2 bg-amber-500 text-black hover:bg-amber-400 focus-visible:ring-amber-300"
        >
          <Play className="size-4" aria-hidden="true" />
          Resume Trading
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Resume live trading?</AlertDialogTitle>
          <AlertDialogDescription>
            This clears the persistent halt flag set by the last kill-all.
            Verify that all positions are flat via the IB portal before
            resuming.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            data-testid="resume-confirm"
            variant="destructive"
            disabled={submitting}
            onClick={handleConfirm}
          >
            {submitting ? "Resuming…" : "Resume"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
