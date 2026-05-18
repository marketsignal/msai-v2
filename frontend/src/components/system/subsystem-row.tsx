"use client";

import { Badge } from "@/components/ui/badge";
import { CheckCircle2, AlertTriangle, HelpCircle } from "lucide-react";
import type { SubsystemStatus } from "@/lib/api";

interface Props {
  name: string;
  status: SubsystemStatus;
}

/**
 * Subsystem row — Trust-First status with color + icon + text + timestamp.
 *
 * Backends may attach arbitrary extras to `SubsystemStatus` (e.g.
 * `queue_depth` for workers, `total_files` for parquet). These render
 * inline below the status text for the most common keys; unknown keys
 * are skipped to avoid leaking internals into the UI.
 */
export function SubsystemRow({ name, status }: Props): React.ReactElement {
  return (
    <div
      className="flex items-start justify-between rounded-md border border-border/50 p-4"
      data-testid="subsystem-row"
      data-subsystem={name}
    >
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <StatusIcon status={status.status} />
          <p className="text-sm font-medium capitalize">{prettify(name)}</p>
          <StatusBadge status={status.status} />
        </div>
        {status.detail && (
          <p className="text-xs text-muted-foreground">{status.detail}</p>
        )}
        <ExtraDetails status={status} />
      </div>
      <p className="shrink-0 font-mono text-xs text-muted-foreground">
        {formatRelative(status.last_checked)}
      </p>
    </div>
  );
}

function prettify(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\bib\b/i, "IB")
    .replace(/\bdb\b/i, "DB");
}

function StatusIcon({ status }: { status: string }): React.ReactElement {
  if (status === "healthy") {
    return (
      <CheckCircle2 className="size-4 text-emerald-400" aria-hidden="true" />
    );
  }
  if (status === "unhealthy") {
    return <AlertTriangle className="size-4 text-red-400" aria-hidden="true" />;
  }
  return (
    <HelpCircle className="size-4 text-muted-foreground" aria-hidden="true" />
  );
}

function StatusBadge({ status }: { status: string }): React.ReactElement {
  const className =
    status === "healthy"
      ? "bg-emerald-500/15 text-emerald-400"
      : status === "unhealthy"
        ? "bg-red-500/15 text-red-400"
        : "bg-muted text-muted-foreground";
  return (
    <Badge variant="secondary" className={className}>
      {status}
    </Badge>
  );
}

function ExtraDetails({
  status,
}: {
  status: SubsystemStatus;
}): React.ReactElement | null {
  const lines: string[] = [];
  if (typeof status.queue_depth === "number") {
    lines.push(`Queue depth: ${status.queue_depth}`);
  }
  if (typeof status.total_files === "number") {
    lines.push(`Files: ${status.total_files}`);
  }
  if (typeof status.total_bytes === "number") {
    lines.push(`Bytes: ${formatBytes(status.total_bytes)}`);
  }
  if (lines.length === 0) return null;
  return (
    <p className="font-mono text-xs text-muted-foreground">
      {lines.join(" · ")}
    </p>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
