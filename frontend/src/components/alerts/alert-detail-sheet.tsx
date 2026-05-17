"use client";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import type { AlertRecord } from "@/lib/api";
import { Bell, AlertTriangle } from "lucide-react";

interface Props {
  /** The currently-selected alert snapshot. `null` closes the sheet. */
  alert: AlertRecord | null;
  onClose: () => void;
}

export function AlertDetailSheet({
  alert,
  onClose,
}: Props): React.ReactElement {
  return (
    <Sheet
      open={alert !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent side="right" className="w-full sm:max-w-md">
        {alert !== null && (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2">
                <LevelBadge level={alert.level} />
                <span>{alert.title}</span>
              </SheetTitle>
              <SheetDescription>
                <time className="font-mono text-xs">{alert.created_at}</time>
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-4 px-4 pb-4 pt-2">
              <Field label="Type" value={alert.type} mono />
              <Field label="Level" value={alert.level} />
              <Field label="Message" value={alert.message} multiline />
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}

function Field({
  label,
  value,
  mono,
  multiline,
}: {
  label: string;
  value: string;
  mono?: boolean;
  multiline?: boolean;
}): React.ReactElement {
  return (
    <div className="space-y-1">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p
        className={[
          "text-sm",
          mono ? "font-mono" : "",
          multiline ? "whitespace-pre-wrap" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {value}
      </p>
    </div>
  );
}

function LevelBadge({ level }: { level: string }): React.ReactElement {
  const normalized = level.toLowerCase();
  const isError = normalized === "error" || normalized === "critical";
  const isWarn = normalized === "warning";
  return (
    <Badge
      variant="secondary"
      className={
        isError
          ? "gap-1 bg-red-500/15 text-red-400"
          : isWarn
            ? "gap-1 bg-amber-500/15 text-amber-400"
            : "gap-1 bg-muted text-muted-foreground"
      }
    >
      {isError || isWarn ? (
        <AlertTriangle className="size-3" aria-hidden="true" />
      ) : (
        <Bell className="size-3" aria-hidden="true" />
      )}
      {level}
    </Badge>
  );
}
