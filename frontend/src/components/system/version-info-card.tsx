"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Server } from "lucide-react";

interface Props {
  version: string;
  commitSha: string;
  uptimeSeconds: number;
}

export function VersionInfoCard({
  version,
  commitSha,
  uptimeSeconds,
}: Props): React.ReactElement {
  return (
    <Card className="border-border/50">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Server className="size-4 text-muted-foreground" aria-hidden="true" />
          <CardTitle className="text-base">Build & uptime</CardTitle>
        </div>
        <CardDescription>
          Real values from <code className="font-mono">pyproject.toml</code> +{" "}
          <code className="font-mono">GITHUB_SHA</code> + process start time. No
          more hardcoded <code className="font-mono">v0.1.0</code>.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-4 sm:grid-cols-3">
          <Stat label="Version" value={version} mono />
          <Stat label="Commit SHA" value={commitSha} mono />
          <Stat label="Uptime" value={formatUptime(uptimeSeconds)} mono />
        </dl>
      </CardContent>
    </Card>
  );
}

function Stat({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): React.ReactElement {
  return (
    <div className="space-y-1">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className={`text-base font-semibold ${mono ? "font-mono" : ""}`}>
        {value}
      </dd>
    </div>
  );
}

function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "unknown";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0 || days > 0) parts.push(`${hours}h`);
  parts.push(`${minutes}m`);
  return parts.join(" ");
}
