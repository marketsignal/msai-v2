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
  ShieldCheck,
  ShieldAlert,
  Pause,
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

/** Compact heartbeat / router age label, or "stale" when unknown (PR 2 T8). */
function ageLabel(ageSeconds: number | null | undefined): string {
  if (ageSeconds === null || ageSeconds === undefined) return "stale";
  return `${ageSeconds.toFixed(1)}s`;
}

/**
 * Router-heartbeat staleness threshold, in seconds (PR 2 F3).
 *
 * The router heartbeat Redis key has a 90s TTL, so `router_heartbeat_age_s`
 * stays numeric for up to 90s after the supervisor dies — but the backend
 * treats the supervisor as DEAD far earlier. This mirrors the backend SPOF
 * threshold `ROUTER_HEARTBEAT_SPOF_THRESHOLD_S` (default 30s, env-overridable
 * via `MSAI_ROUTER_SPOF_THRESHOLD_S`) in
 * `backend/src/msai/services/fleet_alerts.py` — the exact age at which the
 * fleet SPOF alert pages. Keep the two in sync so the dashboard, the CLI
 * (`msai live status`), and the backend alert all agree on "dead" rather than
 * showing "alive (45s ago)" for a fleet that is actually unmonitored.
 */
const ROUTER_HEARTBEAT_SPOF_THRESHOLD_S = 30;

/**
 * PR 2 T8 — per-account restart-authority health cell.
 *
 * Surfaces, for each deployment: whether the bounded auto-restart policy has
 * tripped (``auto_restart_paused`` — the operator MUST intervene), the
 * consecutive-respawn-failure count, the node heartbeat age, and the live
 * fleet/account halt-latch state. A paused account or an active halt is
 * red-tinted so the operator can spot an account that needs attention without
 * leaving the dashboard. Read-only — additive to the existing table.
 */
function SupervisorHealthCell({
  dep,
}: {
  dep: LiveDeploymentInfo;
}): React.ReactElement {
  const paused = dep.auto_restart_paused === true;
  const halted = dep.fleet_halted || dep.account_halted;
  const failures = dep.consecutive_respawn_failures ?? 0;
  const attention = paused || halted;

  const haltLabels: string[] = [];
  if (dep.fleet_halted) haltLabels.push("fleet");
  if (dep.account_halted) haltLabels.push("account");

  return (
    <div
      data-testid={`supervisor-health-${dep.id}`}
      className="flex flex-col gap-0.5 text-xs"
    >
      <span
        className={
          attention
            ? "flex items-center gap-1 font-medium text-red-400"
            : "flex items-center gap-1 text-emerald-500"
        }
      >
        {paused ? (
          <>
            <Pause className="size-3" aria-hidden="true" />
            Restart paused
          </>
        ) : attention ? (
          <>
            <ShieldAlert className="size-3" aria-hidden="true" />
            Halted
          </>
        ) : (
          <>
            <ShieldCheck className="size-3" aria-hidden="true" />
            Auto-restart on
          </>
        )}
      </span>
      {paused && dep.auto_restart_pause_reason ? (
        <span
          className="text-muted-foreground"
          title={dep.auto_restart_pause_reason}
        >
          {dep.auto_restart_pause_reason}
        </span>
      ) : null}
      <span className="text-muted-foreground">
        {failures > 0 ? (
          <span className="text-amber-500">{failures} fail(s)</span>
        ) : (
          "0 fail(s)"
        )}{" "}
        · hb {ageLabel(dep.last_heartbeat_age_s)}
        {haltLabels.length > 0 ? (
          <>
            {" "}
            · <span className="text-red-400">halt: {haltLabels.join("+")}</span>
          </>
        ) : null}
      </span>
    </div>
  );
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
  /**
   * Pablo 2026-05-17: the table previously rendered raw strategy_id
   * UUIDs in the Strategy column ("6bfccf42-2765-…"), forcing the
   * trader to mentally map UUIDs to names. Parent passes a UUID → name
   * map so we can show ``example.smoke_market_order`` instead. Fallback
   * to the UUID prefix if the name is missing (deployment for an
   * archived strategy whose row is no longer in the live list).
   */
  strategiesById?: Record<string, string>;
  /**
   * PR 2 T8 — age in seconds of the supervisor's ``router_heartbeat``
   * (from the top level of /live/status). The single live-supervisor is a
   * SPOF; null means it is down / never started (fail-closed). Surfaced in
   * the card header so the operator can confirm the fleet is being
   * monitored at a glance. Undefined while the parent hasn't fetched yet.
   */
  routerHeartbeatAgeS?: number | null;
}

export function StrategyStatus({
  deployments,
  onDeploymentMutated,
  strategiesById,
  routerHeartbeatAgeS,
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
        {/* PR 2 T8 — supervisor (router) liveness. The single live-supervisor
            is a SPOF; a stale/absent heartbeat means nothing is reaping or
            auto-restarting crashed nodes.

            PR 2 F3 — the heartbeat key's 90s TTL keeps the age numeric for up
            to 90s after the supervisor dies, but the backend treats it as DEAD
            at the SPOF threshold (30s). Render STALE (warning) once the age
            exceeds that threshold — NOT just when it's null — so the dashboard
            never shows "alive (45s ago)" for an unmonitored fleet. */}
        {routerHeartbeatAgeS !== undefined
          ? (() => {
              const isDown = routerHeartbeatAgeS === null;
              const isStale =
                routerHeartbeatAgeS !== null &&
                routerHeartbeatAgeS > ROUTER_HEARTBEAT_SPOF_THRESHOLD_S;
              const unhealthy = isDown || isStale;
              return (
                <p
                  data-testid="supervisor-router-health"
                  className={
                    unhealthy
                      ? "flex items-center gap-1 text-xs font-medium text-red-400"
                      : "flex items-center gap-1 text-xs text-muted-foreground"
                  }
                >
                  {isDown ? (
                    <>
                      <ShieldAlert className="size-3" aria-hidden="true" />
                      Supervisor: DOWN (no heartbeat) — fleet unmonitored
                    </>
                  ) : isStale ? (
                    <>
                      <ShieldAlert className="size-3" aria-hidden="true" />
                      Supervisor: STALE ({ageLabel(routerHeartbeatAgeS)} ago) —
                      fleet unmonitored
                    </>
                  ) : (
                    <>
                      <ShieldCheck className="size-3" aria-hidden="true" />
                      Supervisor: alive ({ageLabel(routerHeartbeatAgeS)} ago)
                    </>
                  )}
                </p>
              );
            })()
          : null}
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
                <TableHead>Account</TableHead>
                <TableHead>Login</TableHead>
                <TableHead className="text-right">Client ID</TableHead>
                <TableHead>Instruments</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Supervisor</TableHead>
                <TableHead>Start Time</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead className="w-44" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {deployments.map((dep) => {
                // strategy_id is `UUID | None` server-side — legacy/partially
                // migrated rows can be null. Guard before slicing.
                const strategyName = dep.strategy_id
                  ? (strategiesById?.[dep.strategy_id] ??
                    `${dep.strategy_id.slice(0, 8)}…`)
                  : "—";
                return (
                  <TableRow
                    key={dep.id}
                    data-testid={`dep-row-${dep.id}`}
                    className="border-border/50"
                  >
                    <TableCell className="font-medium">
                      <span title={dep.strategy_id ?? "no strategy_id"}>
                        {strategyName}
                      </span>
                    </TableCell>
                    {/* PR 1 T14 — account context for the fleet topology.
                        data-testid hooks (Codex iter-23 verify-e2e
                        recommendation) give Phase 6.2c spec generation
                        deterministic selectors that don't drift on
                        column reordering. */}
                    <TableCell
                      data-testid={`dep-account-${dep.id}`}
                      className="font-mono text-xs"
                    >
                      {dep.account_id ?? "—"}
                    </TableCell>
                    <TableCell
                      data-testid={`dep-login-${dep.id}`}
                      className="font-mono text-xs"
                    >
                      {dep.ib_login_key ?? "—"}
                    </TableCell>
                    <TableCell
                      data-testid={`dep-ibg-client-${dep.id}`}
                      className="text-right font-mono text-xs"
                    >
                      {dep.ibg_client_id ?? "—"}
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
                    {/* PR 2 T8 — per-account restart-authority health.
                        data-testid hook lets the eventual E2E spec select
                        the cell deterministically. */}
                    <TableCell>
                      <SupervisorHealthCell dep={dep} />
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
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
