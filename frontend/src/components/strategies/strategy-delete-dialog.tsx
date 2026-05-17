"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

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
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Trash2 } from "lucide-react";

import {
  deleteStrategy,
  describeApiError,
  type StrategyResponse,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

interface Props {
  strategy: StrategyResponse;
}

export function StrategyDeleteDialog({ strategy }: Props): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [confirmName, setConfirmName] = useState("");

  const mutation = useMutation({
    mutationFn: async (): Promise<{ message: string }> => {
      const token = await getToken();
      return deleteStrategy(strategy.id, token);
    },
    // NON-optimistic per research finding 6: wait for 200, then invalidate.
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["strategies"] });
      toast.success("Strategy archived", {
        description: `${strategy.name} — historical backtests remain accessible.`,
      });
      setOpen(false);
      router.push("/strategies");
    },
    onError: (err) => {
      toast.error("Archive failed", {
        description: describeApiError(err, "Archive failed"),
      });
    },
  });

  const matches = confirmName.trim() === strategy.name;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) setConfirmName("");
      }}
    >
      <AlertDialogTrigger asChild>
        <Button
          variant="destructive"
          size="sm"
          className="gap-2"
          data-testid="strategy-delete-trigger"
        >
          <Trash2 className="size-4" aria-hidden="true" />
          Archive strategy
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Archive this strategy?</AlertDialogTitle>
          <AlertDialogDescription>
            Soft-delete{" "}
            <span className="font-mono font-medium text-foreground">
              {strategy.name}
            </span>
            . The strategy disappears from new operations (backtests, research,
            new deployments) but historical backtests and any currently-running
            deployments keep working. This action is reversible only by manual
            DB restore — proceed deliberately.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-2">
          <Label htmlFor="confirm-strategy-name">
            Type <span className="font-mono">{strategy.name}</span> to confirm
          </Label>
          <Input
            id="confirm-strategy-name"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            placeholder={strategy.name}
            autoComplete="off"
            data-testid="strategy-delete-confirm-input"
          />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>
            Cancel
          </AlertDialogCancel>
          <AlertDialogAction
            disabled={!matches || mutation.isPending}
            onClick={(e) => {
              e.preventDefault();
              mutation.mutate();
            }}
            className="bg-red-500/90 text-red-50 hover:bg-red-500"
            data-testid="strategy-delete-confirm"
          >
            {mutation.isPending ? "Archiving…" : "Archive strategy"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
