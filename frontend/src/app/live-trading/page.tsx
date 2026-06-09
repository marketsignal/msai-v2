"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TrendingUp, DollarSign, Wifi, AlertTriangle } from "lucide-react";
import { KillSwitch } from "@/components/live/kill-switch";
import { ResumeButton } from "@/components/live/resume-button";
import { StrategyStatus } from "@/components/live/strategy-status";
import { PositionsTable } from "@/components/live/positions-table";
import {
  apiGet,
  describeApiError,
  getLivePositions,
  getLiveStatus,
  type LivePositionItem,
  type LiveDeploymentInfo,
  type StrategyListResponse,
} from "@/lib/api";
import { formatCurrency, formatSignedCurrency } from "@/lib/format";
import { useAuth } from "@/lib/auth";
import { useLiveStream } from "@/lib/use-live-stream";
import { useAccountScope } from "@/lib/account-scope";

export default function LiveTradingPage(): React.ReactElement {
  const { getToken } = useAuth();
  const { scope } = useAccountScope();
  // Codex iter-4 P2: track token resolution separately from the value.
  // getToken() returns null in API-key-only dev mode (NEXT_PUBLIC_MSAI_API_KEY
  // fallback). The previous `if (token === null) return` guards blocked the
  // /live/status and /live/positions loads forever in that setup, hiding
  // the risk_halted banner + ResumeButton entirely.
  const [token, setToken] = useState<string | null>(null);
  const [tokenReady, setTokenReady] = useState<boolean>(false);
  const [apiError, setApiError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async (): Promise<void> => {
      const t = await getToken();
      if (!cancelled) {
        setToken(t);
        setTokenReady(true);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [getToken]);

  // Fetch real deployments + global state from /api/v1/live/status.
  // `deployments` is the DEFAULT (all-status, 50-most-recent) list — it drives
  // the deployment TABLE, which is built to show running AND stopped/failed rows
  // (audit log + actions). `activeDeployments` is the uncapped ACTIVE-only set —
  // it drives anything that must be COMPLETE: the position→account scoping map,
  // the fleet active-count, the WS stream target, and the KillSwitch blast
  // radius. (Codex code-review iter-8 P2: a single activeOnly fetch dropped
  // stopped rows from the table; the default-capped fetch can drop a long-running
  // active deployment from scoping — so we fetch BOTH and use each where correct.)
  const [deployments, setDeployments] = useState<LiveDeploymentInfo[]>([]);
  const [activeDeployments, setActiveDeployments] = useState<
    LiveDeploymentInfo[]
  >([]);
  const [riskHalted, setRiskHalted] = useState<boolean>(false);
  // PR 2 T8 — supervisor (router) liveness age, surfaced on the deployments
  // card so the operator can see the single-supervisor SPOF is alive.
  //
  // PR 2 F5 (review P3): the THREE states are distinct and the child component
  // (StrategyStatus / SupervisorHealthCell) relies on them:
  //   - undefined → NOT yet fetched → render nothing (no premature verdict)
  //   - null      → fetched, heartbeat ABSENT → "Supervisor: DOWN"
  //   - number    → fetched, age in seconds → alive / stale-by-threshold
  // Initialise as `undefined` (not `null`), so the page does NOT flash
  // "Supervisor: DOWN (no heartbeat)" on every load before the first
  // /live/status returns — and a slow/erroring first request doesn't show a
  // FALSE critical supervisor-down signal.
  const [routerHeartbeatAgeS, setRouterHeartbeatAgeS] = useState<
    number | null | undefined
  >(undefined);
  // iter-3 SF P1: positionsUnavailable distinguishes "no positions" (real
  // empty state) from "live/positions fetch failed". Passed to PositionsTable
  // + factored into KillSwitch's positionCount so the operator isn't told
  // "0 positions" when the backend was actually unreachable.
  const [positionsUnavailable, setPositionsUnavailable] =
    useState<boolean>(false);

  // Pablo 2026-05-17: fetch strategies so we can translate `strategy_id`
  // UUIDs to human-readable names in the deployments table. /live/status
  // only returns the UUID; joining client-side keeps the backend contract
  // additive-only.
  const [strategiesById, setStrategiesById] = useState<Record<string, string>>(
    {},
  );
  useEffect(() => {
    if (!tokenReady) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const data = await apiGet<StrategyListResponse>(
          "/api/v1/strategies/",
          token,
        );
        if (cancelled) return;
        const map: Record<string, string> = {};
        for (const s of data.items) map[s.id] = s.name;
        setStrategiesById(map);
      } catch {
        // Non-blocking: deployments still render with UUID-prefix
        // fallback when the strategies fetch fails.
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token, tokenReady]);

  const refreshStatus = useCallback(async (): Promise<void> => {
    if (!tokenReady) return;
    try {
      // Fetch BOTH lists (Codex code-review iter-8 P2):
      //   - default (all-status, 50-recent) → the deployment table (keeps
      //     stopped/failed rows visible, as StrategyStatus is designed for).
      //   - active_only (uncapped) → position→account scoping, fleet counts,
      //     WS target, KillSwitch — these must be COMPLETE; the 50-cap could
      //     drop a long-running active deployment and silently zero its scoped
      //     positions / undercount the blast radius.
      const [status, activeStatus] = await Promise.all([
        getLiveStatus(token),
        getLiveStatus(token, { activeOnly: true }),
      ]);
      setDeployments(status.deployments);
      setActiveDeployments(activeStatus.deployments);
      setRiskHalted(status.risk_halted);
      setRouterHeartbeatAgeS(status.router_heartbeat_age_s);
      setApiError(null);
    } catch (err) {
      // iter-3 SF P1: bare catch swallowed the real cause. The
      // /live/status endpoint drives the risk-halted banner + Resume
      // button + active-deployment count — a 5xx silently degrading to
      // "no deployments" was actively misleading. Surface the detail
      // via describeApiError.
      //
      // Codex code-review iter-4 P2: PRESERVE the last-known deployments on a
      // status-fetch error rather than clearing to []. Clearing emptied
      // `scopedDeploymentIds`, which made the account-scoped P&L/positions
      // filter drop EVERY position (showing a misleading $0 for the selected
      // account) even when `/live/positions` itself succeeded — while the "All"
      // view still showed positions. Keeping the last map lets scoping keep
      // resolving; the apiError banner below signals the staleness (the same
      // preserve-and-warn contract `restPositions` already uses on its own
      // fetch error).
      setApiError(describeApiError(err, "Failed to load deployments"));
    }
  }, [token, tokenReady]);

  useEffect(() => {
    if (!tokenReady) return;
    void refreshStatus();
  }, [token, tokenReady, refreshStatus]);

  // REST fallback: fetch positions when WebSocket not yet connected
  const [restPositions, setRestPositions] = useState<LivePositionItem[]>([]);
  useEffect(() => {
    if (!tokenReady) return;
    let cancelled = false;
    void (async (): Promise<void> => {
      try {
        const data = await getLivePositions(token);
        if (!cancelled) {
          setRestPositions(data.positions);
          setPositionsUnavailable(false);
        }
      } catch (err) {
        // iter-3 SF P1: bare catch + "leave empty" comment was exactly
        // the silent-failure pattern. positionsUnavailable flag now
        // distinguishes a real empty list from a fetch failure so
        // KillSwitch's positionCount doesn't lie ("0 positions" → kill
        // is safe vs. "?" → unknown).
        if (!cancelled) {
          setRestPositions([]);
          setPositionsUnavailable(true);
          console.error(
            "live_positions_fetch_failed",
            describeApiError(err, "REST positions fetch failed"),
          );
        }
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [token, tokenReady]);

  // WS target + fleet counts come from the COMPLETE active set, not the
  // 50-capped all-status `deployments` (Codex iter-8 P2).
  const activeRealDeployment = activeDeployments.find(
    (d) => d.status === "running",
  );
  const live = useLiveStream(activeRealDeployment?.id ?? null, { token });

  const isConnected = live.connectionState === "open";
  const usingLive = isConnected;
  const livePositions = live.positions;

  // Positions for the table: WebSocket > REST > empty
  const positionsForTable = usingLive ? livePositions : restPositions;

  // PR4 (US-001): scoped DISPLAY views — the deployment table + P&L cards +
  // positions table re-scope to the global account selector. The emergency
  // KillSwitch + WS stream wiring below stay FLEET-WIDE (activeCount /
  // positionCount / activeRealDeployment unchanged) so the kill blast radius
  // is never understated (iter-3/4 P1).
  // Table set: filter the ALL-STATUS `deployments` (keeps stopped/failed rows).
  const scopedDeployments = useMemo(() => {
    if (scope === "all") return deployments;
    if (scope === "unassigned") return deployments.filter((d) => !d.account_id);
    return deployments.filter((d) => d.account_id === scope);
  }, [deployments, scope]);

  // Position-scoping id set: from the COMPLETE ACTIVE set (open positions belong
  // to active deployments; using the 50-capped table set could drop one and
  // silently zero its positions — Codex iter-8 P2).
  const scopedActiveDeploymentIds = useMemo(() => {
    const inScope =
      scope === "all"
        ? activeDeployments
        : scope === "unassigned"
          ? activeDeployments.filter((d) => !d.account_id)
          : activeDeployments.filter((d) => d.account_id === scope);
    return new Set(inScope.map((d) => d.id));
  }, [activeDeployments, scope]);

  // PR4 scoped DISPLAY positions. `restPositions` (GET /live/positions) is the
  // fleet-complete source; `livePositions` (WS) is live but covers ONLY the
  // single streamed `activeRealDeployment`.
  //   - scope === "all": today's behavior (positionsForTable, WS-preferred).
  //   - scoped: REST fleet filtered to the scoped account, BUT when the WS
  //     stream IS connected to a deployment within the scoped set, overlay its
  //     LIVE rows (replacing that deployment's stale mount-time REST rows).
  //     This makes a scoped view exactly as fresh as the "all" view for the
  //     streamed deployment (Codex code-review P2 — scoped views must not be
  //     staler than "all"). Non-streamed deployments use REST, the same
  //     freshness "all" has when its WS is down — a pre-existing page property,
  //     not introduced here.
  const scopedPositions = useMemo(() => {
    if (scope === "all") return positionsForTable;
    const restScoped = restPositions.filter((p) =>
      scopedActiveDeploymentIds.has(p.deployment_id),
    );
    const streamedId = activeRealDeployment?.id;
    if (usingLive && streamedId && scopedActiveDeploymentIds.has(streamedId)) {
      const others = restScoped.filter((p) => p.deployment_id !== streamedId);
      const liveForStreamed = livePositions.filter(
        (p) => p.deployment_id === streamedId,
      );
      return [...others, ...liveForStreamed];
    }
    return restScoped;
  }, [
    scope,
    positionsForTable,
    restPositions,
    livePositions,
    usingLive,
    activeRealDeployment,
    scopedActiveDeploymentIds,
  ]);

  // Does the WS stream actually cover EVERY position in the current view?
  // The WS supplies positions for ONLY the single `activeRealDeployment`. So it
  // "covers the view" only when every in-scope ACTIVE deployment IS that one
  // streamed deployment — i.e. the scoped active set is ⊆ {streamedId}. If the
  // scope (or "All") contains 2+ active deployments, the stream is PARTIAL: it
  // has the streamed deployment's rows but not the others'. When REST then fails
  // (`positionsUnavailable`), `scopedPositions` would silently under-report the
  // non-streamed deployments — so the unavailable banner MUST still fire.
  // (Codex code-review iter-6 P2 surfaced the scoped-non-streamed gap; the
  // committed-diff review caught that a partial multi-deployment stream was
  // wrongly treated as full coverage.)
  const streamedId = activeRealDeployment?.id;
  const streamCoversView =
    usingLive &&
    streamedId != null &&
    (scopedActiveDeploymentIds.size === 0 ||
      (scopedActiveDeploymentIds.size === 1 &&
        scopedActiveDeploymentIds.has(streamedId)));
  const viewPositionsUnavailable = positionsUnavailable && !streamCoversView;

  const totalUnrealizedPnl = useMemo(() => {
    return scopedPositions.reduce(
      (sum, p) => sum + parseFloat(p.unrealized_pnl),
      0,
    );
  }, [scopedPositions]);

  const totalMarketValue = useMemo(() => {
    return scopedPositions.reduce(
      (sum, p) => sum + parseFloat(p.qty) * parseFloat(p.avg_price),
      0,
    );
  }, [scopedPositions]);

  const totalDailyPnl = useMemo(() => {
    // For "all" preserve today's behavior exactly: daily P&L is the realized
    // P&L of the live WS stream, 0 when no stream is connected. When scoped,
    // sum realized P&L from the scoped (REST fleet) positions for that account.
    if (scope === "all") {
      return usingLive
        ? livePositions.reduce((sum, p) => sum + parseFloat(p.realized_pnl), 0)
        : 0;
    }
    return scopedPositions.reduce(
      (sum, p) => sum + parseFloat(p.realized_pnl),
      0,
    );
  }, [scope, usingLive, livePositions, scopedPositions]);

  // Fleet active-count for the KillSwitch — from the COMPLETE active set so the
  // emergency-stop blast radius is never undercounted by the 50-cap (iter-8 P2).
  const activeCount = activeDeployments.filter(
    (d) => d.status === "running",
  ).length;
  const positionCount = positionsForTable.length;

  return (
    <div className="space-y-6">
      {/* "Deploy New Portfolio" entry point intentionally REMOVED. The
          /live-trading/portfolio compose route is hard-disabled (returns
          404) per council verdict 2026-05-17 — see
          docs/decisions/2026-05-17-portfolio-backtest-deferred.md. Live
          deployments today are seeded via the public API + Phase 1 git-
          file strategy registration; portfolio-from-UI compose is queued
          for a dedicated /new-feature portfolio-backtest PR. */}

      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              Live Trading
            </h1>
            <div className="flex items-center gap-1.5">
              <div
                className={`size-2 rounded-full ${
                  isConnected ? "bg-emerald-500 animate-pulse" : "bg-red-500"
                }`}
              />
              <span className="text-xs text-muted-foreground">
                {isConnected ? "Connected" : "Disconnected"}
              </span>
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            Manage live deployments and monitor open positions
          </p>
        </div>

        <div className="flex flex-col items-end gap-3">
          <div className="flex items-center gap-2">
            <ResumeButton
              riskHalted={riskHalted}
              onResumed={() => {
                void refreshStatus();
              }}
            />
            <KillSwitch
              activeCount={activeCount}
              positionCount={positionCount}
              positionsUnavailable={!usingLive && positionsUnavailable}
              onKilled={() => {
                void refreshStatus();
              }}
            />
          </div>
        </div>
      </div>

      {riskHalted ? (
        <div
          data-testid="risk-halted-banner"
          role="alert"
          className="flex items-start gap-2 rounded-md border border-red-500/50 bg-red-500/15 p-3 text-sm text-red-200"
        >
          <AlertTriangle
            className="mt-0.5 size-4 shrink-0 text-red-300"
            aria-hidden="true"
          />
          <span>
            <strong className="font-semibold">Trading halted.</strong> Resume
            required before new deployments can start.
          </span>
        </div>
      ) : null}

      {/* Summary cards — Pablo 2026-05-17 clarification: these P&L
          values come from MSAI's running deployments' positions, NOT
          the full IB account. With zero running deployments the cards
          show $0.00 which is honest but confusing if you remember the
          $254k IB balance from /dashboard. Subtitle explicitly tells
          the operator which slice they're seeing. */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Daily P&L
            </CardTitle>
            <TrendingUp className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                totalDailyPnl >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatSignedCurrency(totalDailyPnl)}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              From running deployments
            </p>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Unrealized P&L
            </CardTitle>
            <DollarSign className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div
              className={`text-2xl font-semibold ${
                totalUnrealizedPnl >= 0 ? "text-emerald-500" : "text-red-500"
              }`}
            >
              {formatSignedCurrency(totalUnrealizedPnl)}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              MSAI positions only — see /account for full IB balance
            </p>
          </CardContent>
        </Card>
        <Card className="border-border/50">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Market Value
            </CardTitle>
            <Wifi className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {formatCurrency(totalMarketValue)}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              MSAI positions only — see /account for full IB balance
            </p>
          </CardContent>
        </Card>
      </div>

      {apiError && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
          {apiError}
        </div>
      )}

      {/* iter-4 SF P2: a /live/positions REST failure was previously
          only visible inside the KillSwitch confirm dialog. Surface it
          at the page top so an operator who never opens the dialog
          isn't misled into thinking the account is flat. */}
      {viewPositionsUnavailable ? (
        <div
          data-testid="live-positions-unavailable-banner"
          role="alert"
          className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-300"
        >
          Open positions could not be loaded
          {scope !== "all" ? " for the selected account" : ""} — the P&amp;L
          cards and table below may be incomplete, and the kill-switch displays
          unverified counts. Confirm flatness via the IB portal before relying
          on the figures.
        </div>
      ) : null}

      <StrategyStatus
        deployments={scopedDeployments}
        strategiesById={strategiesById}
        routerHeartbeatAgeS={routerHeartbeatAgeS}
        onDeploymentMutated={() => void refreshStatus()}
      />
      <PositionsTable livePositions={scopedPositions} />
    </div>
  );
}
