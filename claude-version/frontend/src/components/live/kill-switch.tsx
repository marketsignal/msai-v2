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
import { OctagonX } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

interface KillSwitchProps {
  activeCount: number;
  positionCount: number;
}

export function KillSwitch({
  activeCount,
  positionCount,
}: KillSwitchProps): React.ReactElement {
  const { getToken } = useAuth();
  const [open, setOpen] = useState(false);

  const handleKillAll = async (): Promise<void> => {
    try {
      const token = await getToken();
      await apiFetch("/api/v1/live/kill-all", { method: "POST" }, token);
    } catch (error) {
      console.error("Kill all failed:", error);
    }
    setOpen(false);
  };

  return (
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
            pending orders, and close all open positions. This action cannot be
            undone.
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
  );
}
