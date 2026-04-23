/**
 * useLiveStream — React hook that connects to the per-deployment
 * live trading WebSocket and exposes the rolling positions /
 * account / connection state to React components.
 *
 * Wire format (matches backend Phase 3 task 3.6):
 *
 *   1. Server sends an initial snapshot:
 *        {"type": "snapshot",
 *         "deployment_id": "...",
 *         "positions": [PositionSnapshot...],
 *         "account": AccountStateUpdate | null}
 *
 *   2. Server then forwards every InternalEvent verbatim from
 *      the projection consumer's pub/sub channel. Each event
 *      is one of (discriminated by ``event_type``):
 *      position_snapshot, fill, order_status, account_state,
 *      risk_halt, deployment_status.
 *
 *   3. Server emits {"type": "heartbeat", "ts": ...} every
 *      30 s if the channel is idle, so the hook can detect
 *      a dead socket.
 *
 * Auth contract (Phase 1 task — pre-existing):
 *   - The first text message MUST be a JWT or API key.
 *   - The hook reads ``NEXT_PUBLIC_MSAI_API_KEY`` from the
 *     environment and sends it as the first message.
 *
 * Reconnect: on close, the hook waits 1 s and reconnects
 * with exponential backoff capped at 30 s. The reconnect
 * loop is cancelled when the React effect unmounts so a
 * page navigation doesn't leak sockets.
 */

"use client";

import { useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Wire types — keep these in sync with backend
// src/msai/services/nautilus/projection/events.py
// ---------------------------------------------------------------------------

export interface PositionSnapshot {
  event_type: "position_snapshot";
  deployment_id: string;
  instrument_id: string;
  qty: string; // Decimal serialized as string
  avg_price: string;
  unrealized_pnl: string;
  realized_pnl: string;
  ts: string; // ISO 8601
}

export interface FillEvent {
  event_type: "fill";
  deployment_id: string;
  client_order_id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  qty: string;
  price: string;
  commission: string;
  ts: string;
}

export interface OrderStatusChange {
  event_type: "order_status";
  deployment_id: string;
  client_order_id: string;
  status:
    | "submitted"
    | "accepted"
    | "filled"
    | "partially_filled"
    | "cancelled"
    | "rejected"
    | "denied";
  reason: string | null;
  ts: string;
}

export interface AccountStateUpdate {
  event_type: "account_state";
  deployment_id: string;
  account_id: string;
  balance: string;
  margin_used: string;
  margin_available: string;
  ts: string;
}

export interface RiskHaltEvent {
  event_type: "risk_halt";
  deployment_id: string;
  reason: string;
  set_at: string;
}

export interface DeploymentStatusEvent {
  event_type: "deployment_status";
  deployment_id: string;
  status:
    | "starting"
    | "building"
    | "ready"
    | "running"
    | "stopping"
    | "stopped"
    | "failed";
  ts: string;
}

export type InternalEvent =
  | PositionSnapshot
  | FillEvent
  | OrderStatusChange
  | AccountStateUpdate
  | RiskHaltEvent
  | DeploymentStatusEvent;

interface SnapshotMessage {
  type: "snapshot";
  deployment_id: string;
  positions: PositionSnapshot[];
  account: AccountStateUpdate | null;
}

interface HeartbeatMessage {
  type: "heartbeat";
  ts: string;
}

// SnapshotMessage and HeartbeatMessage are referenced in
// applyMessage via raw["type"] checks; the union exists in
// the types only — applyMessage casts via ``unknown``.
export type ControlMessage = SnapshotMessage | HeartbeatMessage;

// ---------------------------------------------------------------------------
// Hook state
// ---------------------------------------------------------------------------

export type ConnectionState = "connecting" | "open" | "closed" | "error";

export interface LiveStreamState {
  connectionState: ConnectionState;
  positions: PositionSnapshot[];
  account: AccountStateUpdate | null;
  halted: boolean;
  haltReason: string | null;
  deploymentStatus: DeploymentStatusEvent["status"] | null;
  /** Most recent fills, capped at 50, newest first. */
  recentFills: FillEvent[];
  /** Most recent order-status transitions, capped at 50. */
  recentOrderStatuses: OrderStatusChange[];
}

const FILL_HISTORY_LIMIT = 50;
const ORDER_STATUS_HISTORY_LIMIT = 50;

const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

/**
 * Build the WebSocket URL. The API base comes from
 * ``NEXT_PUBLIC_API_URL`` and is rewritten from ``http(s)``
 * to ``ws(s)`` so the same env var drives both REST and WS.
 */
function buildWsUrl(deploymentId: string): string {
  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8800";
  const wsBase = base.replace(/^http/i, "ws");
  return `${wsBase}/api/v1/live/stream/${deploymentId}`;
}

function getDevApiKey(): string {
  // ``NEXT_PUBLIC_*`` is exposed to the browser bundle. We
  // ONLY honor this in development mode — in production, the
  // hook MUST receive a JWT via the ``token`` option from
  // the auth context. Codex batch 9 P1: silently falling
  // back to a public API key in production would expose
  // every browser visitor's session to the operator's
  // credentials.
  if (process.env.NODE_ENV !== "production") {
    return process.env.NEXT_PUBLIC_MSAI_API_KEY ?? "";
  }
  return "";
}

interface UseLiveStreamOptions {
  /** Pass an explicit JWT to override the dev API-key
   * fallback. In production this is REQUIRED — the hook
   * refuses to connect without it. */
  token?: string | null;
}

/**
 * React hook that subscribes to the live event stream for one
 * deployment and exposes the rolling state. Components only
 * need to pass the deployment id; the hook handles connect,
 * reconnect, snapshot, event dispatch, and cleanup.
 */
export function useLiveStream(
  deploymentId: string | null,
  options: UseLiveStreamOptions = {},
): LiveStreamState {
  const [state, setState] = useState<LiveStreamState>({
    connectionState: "connecting",
    positions: [],
    account: null,
    halted: false,
    haltReason: null,
    deploymentStatus: null,
    recentFills: [],
    recentOrderStatuses: [],
  });

  // Use a ref so the cleanup function in the effect can read
  // the current attempt count without forcing a re-render.
  const reconnectAttemptRef = useRef(0);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const cancelledRef = useRef(false);

  useEffect(() => {
    if (!deploymentId) {
      return;
    }

    // Resolve the auth token ONCE at effect-mount time. If
    // there is no token (no JWT from props, no dev API key),
    // refuse to connect rather than spinning the reconnect
    // loop forever sending empty strings — Codex batch 9 P1.
    const token = options.token ?? getDevApiKey();
    if (!token) {
      setState((prev) => ({ ...prev, connectionState: "error" }));
      return;
    }

    cancelledRef.current = false;

    function connect(): void {
      if (cancelledRef.current || !deploymentId) {
        return;
      }
      setState((prev) => ({ ...prev, connectionState: "connecting" }));

      const url = buildWsUrl(deploymentId);
      const ws = new WebSocket(url);
      socketRef.current = ws;

      ws.onopen = (): void => {
        // Reset backoff on successful connect — only after the
        // server accepts the auth token will we know the
        // connection is fully usable, but ``onopen`` firing is
        // a strong signal that the URL + TCP path are fine.
        reconnectAttemptRef.current = 0;
        ws.send(token);
        setState((prev) => ({ ...prev, connectionState: "open" }));
      };

      ws.onmessage = (event: MessageEvent<string>): void => {
        let data: unknown;
        try {
          data = JSON.parse(event.data);
        } catch {
          // Bad payload — drop it. Logging in production but
          // we don't want a single bad message to crash the
          // hook for the user.
          return;
        }
        setState((prev) => applyMessage(prev, data));
      };

      ws.onerror = (): void => {
        setState((prev) => ({ ...prev, connectionState: "error" }));
      };

      ws.onclose = (): void => {
        socketRef.current = null;
        if (cancelledRef.current) {
          return;
        }
        setState((prev) => ({ ...prev, connectionState: "closed" }));
        scheduleReconnect();
      };
    }

    function scheduleReconnect(): void {
      const attempt = reconnectAttemptRef.current;
      const delay = Math.min(
        RECONNECT_INITIAL_MS * Math.pow(2, attempt),
        RECONNECT_MAX_MS,
      );
      reconnectAttemptRef.current = attempt + 1;
      reconnectTimerRef.current = window.setTimeout(connect, delay);
    }

    connect();

    return (): void => {
      cancelledRef.current = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [deploymentId, options.token]);

  return state;
}

// ---------------------------------------------------------------------------
// Pure message dispatch — exported for unit tests
// ---------------------------------------------------------------------------

/**
 * Apply one wire message (snapshot, heartbeat, or
 * InternalEvent) to the rolling state. Pure function so unit
 * tests can drive it without standing up a real WebSocket.
 */
export function applyMessage(
  state: LiveStreamState,
  raw: unknown,
): LiveStreamState {
  if (!isObject(raw)) {
    return state;
  }

  // Heartbeat — no state change, just acknowledged
  if (raw["type"] === "heartbeat") {
    return state;
  }

  // Snapshot — replace positions + account
  if (raw["type"] === "snapshot") {
    const snapshot = raw as unknown as SnapshotMessage;
    return {
      ...state,
      positions: snapshot.positions ?? [],
      account: snapshot.account ?? null,
    };
  }

  // Otherwise dispatch by InternalEvent.event_type
  const eventType = raw["event_type"];
  if (typeof eventType !== "string") {
    return state;
  }

  switch (eventType) {
    case "position_snapshot":
      return applyPosition(state, raw as unknown as PositionSnapshot);
    case "fill":
      return applyFill(state, raw as unknown as FillEvent);
    case "order_status":
      return applyOrderStatus(state, raw as unknown as OrderStatusChange);
    case "account_state":
      return {
        ...state,
        account: raw as unknown as AccountStateUpdate,
      };
    case "risk_halt": {
      const halt = raw as unknown as RiskHaltEvent;
      return {
        ...state,
        halted: true,
        haltReason: halt.reason,
      };
    }
    case "deployment_status": {
      const status = raw as unknown as DeploymentStatusEvent;
      return {
        ...state,
        deploymentStatus: status.status,
      };
    }
    default:
      return state;
  }
}

function applyPosition(
  state: LiveStreamState,
  position: PositionSnapshot,
): LiveStreamState {
  // Replace any existing snapshot for the same instrument; drop
  // closed positions (qty == 0) so the table only shows open
  // positions — matches the backend's PositionReader fast path.
  const filtered = state.positions.filter(
    (p) => p.instrument_id !== position.instrument_id,
  );
  const isClosed = parseFloat(position.qty) === 0;
  return {
    ...state,
    positions: isClosed ? filtered : [...filtered, position],
  };
}

function applyFill(state: LiveStreamState, fill: FillEvent): LiveStreamState {
  return {
    ...state,
    recentFills: [fill, ...state.recentFills].slice(0, FILL_HISTORY_LIMIT),
  };
}

function applyOrderStatus(
  state: LiveStreamState,
  change: OrderStatusChange,
): LiveStreamState {
  return {
    ...state,
    recentOrderStatuses: [change, ...state.recentOrderStatuses].slice(
      0,
      ORDER_STATUS_HISTORY_LIMIT,
    ),
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
