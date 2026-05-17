"use client";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Square,
  CheckCircle2,
  AlertTriangle,
  CircleDashed,
  DollarSign,
  TestTube2,
} from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuth } from "@/lib/auth";
import {
  stopDeployment,
  describeApiError,
  type LiveDeploymentInfo,
  type LiveStopResponse,
} from "@/lib/api";
import { formatTimestamp } from "@/lib/format";
import { AuditLogSheet } from "@/components/live/audit-log-sheet";

function statusColor(status: string): string {
  switch (status) {
    case "running":
      return "gap-1 bg-emerald-500/15 text-emerald-500 hover:bg-emerald-500/25";
    case "stopped":
      return "gap-1 bg-muted text-muted-foreground hover:bg-muted";
    case "error":
    case "failed":
      return "gap-1 bg-red-500/15 text-red-500 hover:bg-red-500/25";
    default:
      return "gap-1 bg-muted text-muted-foreground hover:bg-muted";
  }
}

function statusIcon(status: string): React.ReactNode {
  // Trust-First: color + icon + text (Code Review iter-1 P1 #3).
  switch (status) {
    case "running":
      return <CheckCircle2 className="size-3" aria-hidden="true" />;
    case "error":
    case "failed":
      return <AlertTriangle className="size-3" aria-hidden="true" />;
    case "stopped":
      return <Square className="size-3" aria-hidden="true" />;
    default:
      return <CircleDashed className="size-3" aria-hidden="true" />;
  }
}

/** Human-readable label for the broker-flatness tri-state returned by Stop. */
function flatnessLabel(res: LiveStopResponse): string {
  if (res.broker_flat === true) return "Broker flat ✓";
  if (res.broker_flat === false) {
    return `Residual positions: ${res.remaining_positions?.length ?? "?"}`;
  }
  return "Flatness unknown (poll timed out)";
}

interface StrategyStatusProps {
  deployments: LiveDeploymentInfo[];
  /**
   * Notify the parent to re-fetch its local deployment state after a
   * successful Stop mutation. The keyed-query invalidation alone is
   * not sufficient because the consuming page (``/live-trading``)
   * uses local ``useState`` rather than a TanStack query — Codex
   * iter-2 P2 #2 caught the toast-success-but-row-stays-running gap.
   */
  onDeploymentMutated?: () => void;
}

export function StrategyStatus({
  deployments,
  onDeploymentMutated,
}: StrategyStatusProps): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();

  // Stop is destructive on real money (Codex iter-1 P0 / silent-failure
  // hunter F1). The previous handler used raw apiFetch without checking
  // res.ok, silently swallowing 4xx/5xx — a trader pressed Stop and had
  // no idea if the kill had landed. Non-optimistic mutation: wait for
  // the 200 + flatness report, surface the result via toast, invalidate
  // status so the row's badge re-renders.
  const stopMutation = useMutation<LiveStopResponse, Error, string>({
    mutationFn: async (deploymentId: string): Promise<LiveStopResponse> => {
      const token = await getToken();
      return stopDeployment(deploymentId, token);
    },
    onSuccess: (res) => {
      toast.success(`Stop sent — status ${res.status}`, {
        description: flatnessLabel(res),
      });
      // Invalidate the TanStack-keyed query (consumers via useQuery) AND
      // call the parent's refresh callback (consumers via local useState,
      // e.g. /live-trading page). Codex iter-2 P2 #2.
      void qc.invalidateQueries({ queryKey: ["live", "status"] });
      onDeploymentMutated?.();
    },
    onError: (err) => {
      // iter-3 describeApiError sweep + SF P2: drop the manual
      // status-code suffix from the title since the description already
      // carries the backend detail. The "(422)" suffix was noise on
      // backend-validated 4xx and only useful on opaque 5xx.
      toast.error("Stop failed", {
        description: describeApiError(err, "Stop request failed"),
      });
    },
  });

  return (
    <Card className="border-border/50">
      <CardHeader>
        <CardTitle className="text-base">Active Deployments</CardTitle>
        <CardDescription>
          Running and stopped strategy deployments
        </CardDescription>
      </CardHeader>
      <CardContent>
        {deployments.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            No deployments.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-border/50 hover:bg-transparent">
                <TableHead>Strategy</TableHead>
                <TableHead>Instruments</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Start Time</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead className="w-44" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {deployments.map((dep) => (
                <TableRow key={dep.id} className="border-border/50">
                  <TableCell className="font-medium">
                    {dep.strategy_id}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(dep.instruments ?? []).map((inst) => (
                        <Badge
                          key={inst}
                          variant="outline"
                          className="text-xs font-normal"
                        >
                          {inst}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={statusColor(dep.status)}
                    >
                      {statusIcon(dep.status)}
                      {dep.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {dep.started_at ? formatTimestamp(dep.started_at) : "--"}
                  </TableCell>
                  <TableCell>
                    {/* Real-money differentiation: Paper (neutral) vs
                        Live (red-tinted) so the mode column visually
                        screams real-money — Code Review iter-1 P1 #3. */}
                    {dep.paper_trading ? (
                      <Badge
                        variant="outline"
                        className="gap-1 text-xs font-normal text-muted-foreground"
                      >
                        <TestTube2 className="size-3" aria-hidden="true" />
                        Paper
                      </Badge>
                    ) : (
                      <Badge
                        variant="secondary"
                        className="gap-1 bg-red-500/15 text-xs font-semibold text-red-400"
                      >
                        <DollarSign className="size-3" aria-hidden="true" />
                        LIVE
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <AuditLogSheet deploymentId={dep.id} />
                      {["starting", "building", "ready", "running"].includes(
                        dep.status,
                      ) && (
                        <Button
                          variant="outline"
                          size="xs"
                          className="gap-1 text-red-400 hover:text-red-300"
                          onClick={() => stopMutation.mutate(dep.id)}
                          disabled={
                            stopMutation.isPending &&
                            stopMutation.variables === dep.id
                          }
                          data-testid={`stop-${dep.id}`}
                        >
                          <Square className="size-3" />
                          {stopMutation.isPending &&
                          stopMutation.variables === dep.id
                            ? "Stopping…"
                            : "Stop"}
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
