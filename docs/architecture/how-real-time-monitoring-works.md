<!-- forge:doc how-real-time-monitoring-works -->

# How Real-Time Monitoring Works

This is the last stop on the developer journey. The portfolio is deployed, the TradingNode subprocess is talking to IB Gateway, and orders are starting to flow. Now you need to **see what's happening** — without polling, without refreshing, without guessing whether the screen is stale.

The mechanism is a WebSocket per deployment with a strict first-message JWT handshake, a DB-replay snapshot on every connect, and Redis pub/sub fan-out so every uvicorn worker can serve every deployment's stream. The dashboard at `:3300/dashboard` and the deployment detail at `:3300/live-trading` are the two UI surfaces. The CLI is **REST-snapshot-only** — there is no streaming on the terminal — and that's deliberate.

This doc closes the loop on doc 7 ([How Live Portfolios and IB Accounts Work](how-live-portfolios-and-ib-accounts.md)). When the kill-all flow you set up there fires in production, this is the doc that tells you where the `risk_halt` and `deployment_status` events surface — primarily on `/live-trading`, since the dashboard is currently a static-on-mount summary.

---

## The Component Diagram

```
                ┌─ TRADINGNODE SUBPROCESS ─────────────┐
                │ (one per deployment, owned by         │
                │  live_supervisor — see doc 7)         │
                │                                       │
                │   on_order_event / on_fill_event /    │
                │   on_account_state / on_status        │
                │                                       │
                └──────────────┬────────────────────────┘
                               │  msgpack via MessageBus
                               ▼
                ┌─ PROJECTION CONSUMER ────────────────┐
                │  reads message bus stream,            │
                │  builds InternalEvent, publishes via  │
                │  DualPublisher to TWO Redis channels  │
                │  per deployment (state + events)      │
                └──────┬─────────────────────┬──────────┘
                       │                     │
                       ▼                     ▼
   ┌──── REDIS PUB/SUB (state) ──────┐  ┌──── REDIS PUB/SUB (events) ─────┐
   │ msai:live:state:{deployment_id} │  │ msai:live:events:{deployment_id}│
   │ PSUBSCRIBE msai:live:state:*    │  │ per-deployment channel sub      │
   │ — every uvicorn worker's        │  │ — only workers with a connected │
   │   StateApplier subscribes       │  │   WS client subscribe           │
   └────┬────────┬────────┬──────────┘  └──┬──────────────────┬───────────┘
        ▼        ▼        ▼                ▼                  ▼
   ┌ worker A ┐ worker B ┐ worker C ┐  ┌ worker A ┐       ┌ worker B ┐
   │ Applier  │ Applier  │ Applier  │  │ WS hndlr │       │ WS hndlr │
   │ updates  │ updates  │ updates  │  │ client 1 │       │ client 2 │
   │ Projec-  │ Projec-  │ Projec-  │  └────┬─────┘       └────┬─────┘
   │ tionState│ tionState│ tionState│       ▼                  ▼
   └──────────┴──────────┴──────────┘  ┌─ Browser 1 ─┐  ┌─ Browser 2 ─┐
                                       │ Dashboard   │  │ live-trading │
                                       │ /dashboard  │  │ page         │
                                       └─────────────┘  └──────────────┘


            ── parallel REST surface, no streaming ──
                          (both API and CLI)

   GET /api/v1/account/summary    →  IB account financial metrics
   GET /api/v1/account/portfolio  →  IB positions (truth from broker)
   GET /api/v1/account/health     →  IB Gateway probe state
   GET /api/v1/live/status        →  Deployment list + statuses
   GET /api/v1/live/positions     →  MSAI's audit view of positions
   GET /api/v1/live/trades        →  Recent executions (audit)
   GET /api/v1/alerts/            →  Operator alert history
```

The producer side (TradingNode → projection consumer → Redis pub/sub) is in process A. The consumer side (FastAPI WebSocket handler → browser) is in process B. They never share memory — Redis is the single source of truth for live events, and Postgres is the single source of truth for the durable audit (orders, trades, deployment state, halt flag).

---

## TL;DR

Real-time monitoring is **observation only** — the WebSocket carries no commands, only events. Every deployment has **two** per-deployment Redis pub/sub channels (`msai:live:state:{id}` for cross-worker `ProjectionState` updates, `msai:live:events:{id}` for WebSocket fan-out — see §1.5). Each uvicorn worker's `StateApplier` subscribes to the state channel at boot; the worker subscribes to the events channel when a client connects to that deployment's stream. On connect the server replays a DB-backed snapshot, then forwards live deltas. **There are three surfaces:**

| Surface | What you get                       | Streaming?                |
| ------- | ---------------------------------- | ------------------------- |
| **API** | WS stream + REST snapshots         | **Yes** (WS) + REST polls |
| **CLI** | REST snapshots                     | **No** — polls only       |
| **UI**  | WS-driven dashboard + live-trading | **Yes** (auto-connect)    |

The CLI is intentionally a REST fallback. There is no `msai live tail` or `msai monitor stream` — if you want the live event feed, open the dashboard or write a script that calls the WebSocket directly. The CLI surface for monitoring is `msai account summary`, `msai account positions`, `msai account health`, `msai live status` — all polling REST endpoints.

---

## Table of Contents

1. [Concepts and Data Model](#1-concepts-and-data-model)
2. [The Three Surfaces](#2-the-three-surfaces)
3. [Internal Sequence Diagrams](#3-internal-sequence-diagrams)
4. [See / Verify / Troubleshoot](#4-see--verify--troubleshoot)
5. [Common Failures](#5-common-failures)
6. [Idempotency and Retry Behavior](#6-idempotency-and-retry-behavior)
7. [Rollback / Repair](#7-rollback--repair)
8. [Key Files](#8-key-files)

---

## 1. Concepts and Data Model

### 1.1 The WebSocket route

```
/api/v1/live/stream/{deployment_id}
```

`deployment_id` is a `LiveDeployment.id` — the UUID returned by `POST /api/v1/live/start-portfolio` (doc 7). Every WS connection is bound to exactly one deployment; if you want to monitor three deployments, you open three sockets.

The route is registered at `backend/src/msai/main.py:284` (`@app.websocket("/api/v1/live/stream/{deployment_id}")`), and the handler is `live_stream` in `backend/src/msai/api/websocket.py:313`. JWT validation (and the API-key fallback) goes through `validate_token_or_api_key` from `core/auth.py`.

### 1.2 The first-message-JWT-within-5-seconds handshake

WebSockets don't carry HTTP headers in a way browsers can set arbitrarily for cross-origin connections, so MSAI does **first-message auth** instead of header auth. The contract:

1. Client opens the socket. Server accepts with `await websocket.accept()`.
2. Client must send **the JWT token (or API key) as the first text message** within `_AUTH_TIMEOUT_SECONDS = 5.0` seconds. (See `api/websocket.py:75`.)
3. Server validates via `validate_token_or_api_key(token)`.
4. On success → server proceeds. On timeout → server closes with code **4001** ("Authentication timed out"). On bad token → server closes with code **4001** ("Invalid token").

The 5-second budget is intentional — long enough that a slow browser can fetch a fresh token from the auth provider, short enough that a port-scanner or misbehaving client can't sit on a half-open socket. The constant is **named** so it survives refactors; if you ever need to change it, change it in one place (`api/websocket.py:75`) and audit the frontend's reconnect logic for matching expectations (`frontend/src/lib/use-live-stream.ts`).

After auth, the handler runs an **authorization** check (`_is_authorized`, `api/websocket.py:279`):

- The API-key dev account (`sub == "api-key-user"`) sees every deployment — single-tenant local mode.
- JWT users see only deployments where `LiveDeployment.started_by` matches the user resolved from the JWT's `sub` claim.
- Anything else → close with code **4403** ("Forbidden").

If the deployment doesn't exist at all → close with code **4404** ("Deployment not found").

### 1.3 The reconnect-hydration pattern

The hardest problem with live event streams is **what does the client see if it just connected?** A naive design forwards events as they arrive, so a fresh client sees nothing until the strategy fires the next event — which on a quiet day might be hours.

MSAI's WebSocket solves this by sending a **DB-backed snapshot** on every connect, before any live deltas. The snapshot composes six fields from a few backing stores (`api/websocket.py:118`):

| Field       | Source                                                                                         | Why this store                                                   |
| ----------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `positions` | `PositionReader.get_open_positions` (in-memory ProjectionState; cold path rebuilds from Redis) | Live positions are derived from cumulative fill events           |
| `account`   | `PositionReader.get_account` (account state event)                                             | Equity, NLV, etc. — comes from IB account update events          |
| `orders`    | `load_open_orders_for_deployment` — open `OrderAttemptAudit` rows from Postgres, newest-first  | The DB is the durable audit; in-memory state vanishes on restart |
| `trades`    | `load_recent_trades_for_deployment` — most recent 50 `Trade` rows from Postgres, newest-first  | Same — the DB survives subprocess restarts                       |
| `status`    | `ProjectionState.get_status(deployment_id)`                                                    | Last seen deployment status event                                |
| `risk_halt` | `ProjectionState.get_halt(deployment_id)` + `is_halted(...)`                                   | Halt flag state — does the strategy currently refuse new orders? |

The snapshot is sent as one JSON message:

```json
{
  "type": "snapshot",
  "deployment_id": "0b2f...c7e9",
  "positions": [...],
  "account": {...},
  "orders": [...],
  "trades": [...],
  "status": {...},
  "risk_halt": {"halted": false, "event": null}
}
```

After the snapshot the server subscribes to the per-deployment Redis pub/sub channel and starts forwarding deltas. On disconnect (any reason — client close, server shutdown, transient network blip), the client reconnects, the **same snapshot read runs again**, and the client gets a fresh point-in-time view. There is no resume-from-offset, no event log replay, no missed-event detection — the snapshot **is** the recovery mechanism.

This is replay-safe because:

- The snapshot is built from durable Postgres tables (`OrderAttemptAudit`, `Trade`) plus an in-memory projection state that itself can be cold-rebuilt from Redis if needed.
- The pub/sub channel is reliable for **connected clients** (they don't miss in-flight events while they're subscribed).
- A client that disconnects and reconnects gets the snapshot — which contains everything it would have missed.

### 1.4 Message types on the wire

Three control-plane message types come through the WS, plus the InternalEvent envelope from the projection consumer:

| `type`         | Direction       | When                                                       |
| -------------- | --------------- | ---------------------------------------------------------- |
| `snapshot`     | server → client | Once per connection, immediately after auth                |
| `heartbeat`    | server → client | Every `_HEARTBEAT_INTERVAL_SECONDS = 30.0` if pub/sub idle |
| `event_type:*` | server → client | Per InternalEvent (forwarded verbatim from pub/sub)        |

The `event_type` discriminator (inside the InternalEvent payload, not on the outer `type`) takes one of:

| `event_type`        | Payload meaning                                                         |
| ------------------- | ----------------------------------------------------------------------- |
| `position_snapshot` | Full position state for one instrument                                  |
| `fill`              | A trade fill (broker_trade_id, side, qty, price, commission, pnl)       |
| `order_status`      | An order status transition (PENDING → FILLED, CANCELED, REJECTED, etc.) |
| `account_state`     | Account update (equity, NLV, margin)                                    |
| `risk_halt`         | Halt-flag state change (set or cleared)                                 |
| `deployment_status` | Deployment status change (running, stopped, failed, …)                  |

The frontend hook (`use-live-stream.ts`) discriminates on `raw["type"]` first (snapshot vs heartbeat vs event), then on `event_type` for the real events.

### 1.5 Dual-channel pub/sub: state + events

FastAPI runs under uvicorn, and uvicorn typically runs N workers behind one load balancer. A single browser is pinned to one worker for the lifetime of the WS connection — fine. But there's no guarantee the producer (the projection consumer in process A) talks to the same worker, **and** every worker keeps its own in-memory `ProjectionState` that has to stay current regardless of which one a future WS client lands on.

**Solution: the projection consumer publishes every event to TWO channels via `DualPublisher`** (`services/nautilus/projection/fanout.py:62`):

| Channel                            | Subscriber                                                                   | Purpose                                                                                  |
| ---------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `msai:live:state:{deployment_id}`  | `StateApplier` task on every uvicorn worker (`PSUBSCRIBE msai:live:state:*`) | Each worker's in-memory `ProjectionState` stays current — fixes cross-worker state drift |
| `msai:live:events:{deployment_id}` | The WebSocket handler that has a connected client for that deployment        | Verbatim fan-out of `InternalEvent.model_dump_json()` to the browser                     |

Both channels carry the **same** serialized `InternalEvent` payload — the consumer publishes once per channel. The split is purely about who's listening.

What this means in practice when worker B accepts a WS client for deployment X:

1. The `StateApplier` on worker B (started on app boot, regardless of any WS connection) is already pattern-subscribed to `msai:live:state:*` and applying every event into its local `ProjectionState`. So `PositionReader.get_open_positions(X)` and `ProjectionState.get_status(X)` return up-to-date values during the snapshot read — even if the projection consumer that produced the events lives in a different process.
2. Worker B opens a Redis pub/sub subscription on `msai:live:events:X` for the WS client.
3. The projection consumer in process A publishes each new event to **both** `msai:live:state:X` and `msai:live:events:X`.
4. Redis fan-outs each publish to every connected subscriber (pub/sub semantics).
5. Worker B's WS handler forwards the events-channel payload to its connected client.

If three different browsers connect to three different workers all watching deployment X, all three workers' `StateApplier`s already see the state stream (started at boot), and all three workers' WS handlers subscribe to the events channel; one publish fan-outs everywhere. There is **no in-memory state shared across workers** — Redis is the single source of truth, and each worker reconstructs the projection independently from the state-channel feed.

The events-channel name is constructed by `events_channel_for(deployment_id)` (`fanout.py:57`); the state-channel name by `state_channel_for(deployment_id)` (`fanout.py:52`). The prefixes are `"msai:live:events:"` and `"msai:live:state:"` (`fanout.py:42, 46`). One channel pair per deployment — never combined.

The consumer ACKs the upstream Nautilus stream entry **only after both publishes succeed**. If either fails, the message stays in the PEL and `XAUTOCLAIM` recovers it on the next consumer pass.

### 1.6 Heartbeat keepalive

`_HEARTBEAT_INTERVAL_SECONDS = 30.0` (`api/websocket.py:79`) is the idle pulse. If the pub/sub channel is quiet for 30 seconds, the server sends `{"type": "heartbeat", "ts": "..."}`. The browser uses this two ways:

- **Liveness check** — if no heartbeats arrive for >60s, the WebSocket is presumed dead (TCP reset or stale NAT mapping); reconnect.
- **Idle-display** — the dashboard shows a "connected" indicator that depends on the heartbeat tick.

The heartbeat task lives in `_heartbeat_loop` (`api/websocket.py:201`) and is cancelled on disconnect via the outer handler's `finally` block.

### 1.7 The parallel REST surface

The WebSocket is for **live event subscription**. Three REST endpoints provide point-in-time **truth from IB**:

| Endpoint                        | Returns                                                              | Source                                 |
| ------------------------------- | -------------------------------------------------------------------- | -------------------------------------- |
| `GET /api/v1/account/summary`   | IB account financial metrics (equity, NLV, buying power)             | Direct IB query via `IBAccountService` |
| `GET /api/v1/account/portfolio` | IB account positions                                                 | Direct IB query                        |
| `GET /api/v1/account/health`    | IB Gateway probe state (`gateway_connected`, `consecutive_failures`) | Background probe task (30s interval)   |

The relationship to MSAI's audit view (`/live/positions`, `/live/trades`):

- **`/account/*`** is **truth from IB** — the broker's view of the account, regardless of which deployment caused which position. If you have positions left over from a manual trade in TWS, they show here.
- **`/live/positions`, `/live/trades`** is **MSAI's audit** — what the supervisor recorded as the result of strategies it ran. This is the deployment-scoped, code-hash-stamped, trader-id-tagged view.

When the two disagree, the IB side is canonical and MSAI's audit needs reconciliation (Nautilus gotcha #19). Today the live-trading page surfaces MSAI's audit view via the WS; the dashboard does not currently render IB-vs-audit divergence side-by-side.

The probe task (`account.py:46`) is started by FastAPI's lifespan handler on app startup and runs `IBProbe.run_periodic(interval=30s)`. The interval is overridable via the `IB_PROBE_INTERVAL_S` env var (`account.py:42-43`). If the lifespan didn't start it, `/account/health` always reports `gateway_connected=false` — the drill on 2026-04-15 documented this exact failure mode three times in a row. Note that `consecutive_failures` is returned **as a string** (the endpoint stringifies the counter), so any UI consumer comparing it numerically must `parseInt` first.

### 1.8 The alerts feed

`GET /api/v1/alerts/` returns a list of operator alerts (newest-first, default 50, clamped `[1, 200]`). Source is a file-backed history written by `alerting_service` whenever the live supervisor, disconnect handler, or any worker emits an alert. It's not pub/sub, not WS — it's a polled snapshot. The store is a single JSON document at `{data_root}/alerts/alerts.json` (`config.py:351 alerts_path`).

No frontend caller polls this endpoint today and no toast UI is mounted (`grep -rn "Toaster\|sonner\|toast(" frontend/src/` returns nothing). The endpoint is the operator's audit trail, intended for `curl | jq` and the future dashboard polling loop. It's intentionally tolerant: a `_HISTORY_WRITE_TIMEOUT_S = 2.0` bounded read (`alerting.py:44`) prevents a wedged writer holding the file lock from hanging callers, and malformed entries are skipped rather than returning 500. The audit log is opportunistic — for guaranteed delivery, watch the WS event stream for `risk_halt` and `deployment_status` instead.

---

## 2. The Three Surfaces

The parity table for monitoring. Note that streaming is **not** on the CLI — that's the only surface that breaks parity, and it's a deliberate scope decision.

| Intent                        | API                                       | CLI                        | UI                                                  | Observe / Verify                                              |
| ----------------------------- | ----------------------------------------- | -------------------------- | --------------------------------------------------- | ------------------------------------------------------------- |
| Connect to live event stream  | `WS /api/v1/live/stream/{deployment_id}`  | **N/A** (no streaming)     | Auto-connect on `/live-trading` page                | Browser devtools → Network → WS tab; `ws_subscribed` log line |
| View account summary          | `GET /api/v1/account/summary`             | `msai account summary`     | Dashboard `<PortfolioSummary>` Total Value tile     | Equity / NLV match what TWS shows                             |
| View positions (IB truth)     | `GET /api/v1/account/portfolio`           | `msai account positions`   | _(no UI surface — REST/CLI only)_                   | Same positions visible in TWS                                 |
| View positions (MSAI audit)   | `GET /api/v1/live/positions`              | _(via `msai live status`)_ | `/live-trading` `<PositionsTable>`                  | Per-deployment-tagged, links back to deployment slug          |
| View IB Gateway health        | `GET /api/v1/account/health`              | `msai account health`      | _(no UI surface — REST/CLI only)_                   | `gateway_connected: true` + `consecutive_failures: "0"`       |
| View recent trades            | `GET /api/v1/live/trades`                 | _(via REST poll)_          | Dashboard `<RecentTrades>` + live-trading WS feed   | Newest 10 (dashboard slice) / 50 (WS snapshot), newest-first  |
| View alert history            | `GET /api/v1/alerts/?limit=50`            | _(via REST poll)_          | _(no UI surface — curl/jq only)_                    | Recent halt/disconnect/exception alerts                       |
| List deployments              | `GET /api/v1/live/status`                 | `msai live status`         | Dashboard `<ActiveStrategies>` + live-trading list  | Status column reflects subprocess state                       |
| Switch active deployment view | _(implicit — one WS per deployment)_      | _(implicit per command)_   | `/live-trading` deployment list rows                | URL/state changes; new WS connection opens                    |
| Detect WS connection state    | _(client-side — exit code on disconnect)_ | **N/A**                    | `useLiveStream` connection state on `/live-trading` | `ws_authenticated` + `ws_subscribed` log lines                |

**Why no CLI streaming?**

The decision is in the project's "API-first, CLI-second, UI-third" ordering rule (CLAUDE.md) and the explicit scope of the CLI sub-app (`cli.py`):

- The CLI exists for **scripting and ops** — kicking off jobs, querying status, dumping snapshots to JSON for piping into `jq`.
- Streaming WebSocket clients in a TUI is a different product (think `htop`-for-trades). Out of scope for Phase 1.
- If you need a terminal feed, `watch -n 5 msai live status` and `tail -f` on the supervisor log are the two primitives that compose into one.
- A future `msai monitor stream` is not blocked — but the WS is the contract, and any CLI client would be a thin wrapper over the same `/api/v1/live/stream/{deployment_id}` endpoint with the same JWT-first-message handshake.

---

## 3. Internal Sequence Diagrams

Two parallel diagrams: one for the consumer side (WS lifecycle as a client experiences it), one for the producer side (how an event from the trading node ends up in the browser).

### 3.1 Consumer side — WS lifecycle from connect to disconnect

```
 Client (browser)         FastAPI worker                Postgres / Redis
 ┌──────────────┐         ┌─────────────────┐          ┌────────────────┐
 │              │         │                 │          │                │
 │  open WS to  │ ──────▶ │ accept()        │          │                │
 │ /api/v1/live │         │                 │          │                │
 │ /stream/{id} │         │                 │          │                │
 │              │         │                 │          │                │
 │  send JWT    │ ──────▶ │ wait_for(text,  │          │                │
 │  (first msg) │         │   timeout=5s)   │          │                │
 │              │         │                 │          │                │
 │              │         │ validate_token  │          │                │
 │              │         │ _or_api_key()   │          │                │
 │              │         │                 │          │                │
 │              │         │ load Deployment │ ───────▶ │  SELECT FROM   │
 │              │         │  by deployment  │          │  live_         │
 │              │         │  _id            │ ◀─────── │  deployments   │
 │              │         │                 │          │                │
 │              │         │ _is_authorized? │          │                │
 │              │         │  (JWT user vs.  │          │                │
 │              │         │   started_by)   │          │                │
 │              │         │                 │          │                │
 │              │         │ HYDRATE:        │          │                │
 │              │         │  positions ←────│ ◀─────── │  ProjectionState│
 │              │         │  account   ←────│ ◀─────── │  (in-memory)   │
 │              │         │  orders    ←────│ ◀─────── │  OrderAttempt  │
 │              │         │  trades    ←────│ ◀─────── │  Trade table   │
 │              │         │  status    ←────│ ◀─────── │  ProjectionState│
 │              │         │  risk_halt ←────│ ◀─────── │  ProjectionState│
 │              │         │                 │          │                │
 │  ◀───────────│ snapshot (one JSON msg)   │          │                │
 │              │         │                 │          │                │
 │              │         │ pubsub.subscribe│ ───────▶ │ msai:live:     │
 │              │         │ (channel)       │          │ events:{id}    │
 │              │         │                 │          │ (channel sub)  │
 │              │         │                 │          │                │
 │              │         │ start heartbeat │          │                │
 │              │         │ task (30s)      │          │                │
 │              │         │                 │          │                │
 │              │         │ start forward   │          │                │
 │              │         │ task            │          │                │
 │              │         │                 │          │                │
 │              │         │   ┌── loop ──┐  │          │                │
 │              │         │   │          │  │ ◀─────── │  PUBLISH event │
 │  ◀───────────│ event payload (JSON)   │  │          │  (fan-out)     │
 │              │         │   │          │  │          │                │
 │  ◀───────────│ {"type":"heartbeat"…}  │  │ (idle ≥30s, no events)    │
 │              │         │   │          │  │          │                │
 │              │         │   └──────────┘  │          │                │
 │              │         │                 │          │                │
 │  X disconnect│ ───────▶│ except          │          │                │
 │              │         │  WebSocketDisc. │          │                │
 │              │         │                 │          │                │
 │              │         │ finally:        │          │                │
 │              │         │  cancel tasks   │          │                │
 │              │         │  unsubscribe    │ ───────▶ │  channel unsub │
 │              │         │  pubsub.close   │          │                │
 └──────────────┘         └─────────────────┘          └────────────────┘
```

Key invariant: **the snapshot is sent before the channel subscription**. The order matters — if you subscribed first and then snapshot'd, you'd risk a race where an event arrives while the snapshot read is in flight, and the client gets the event before the snapshot. That would scramble the merge logic on the frontend (which assumes the snapshot is the baseline, with events as deltas applied on top).

The actual code subscribes after the snapshot is sent (`api/websocket.py:386`). There is a small window between snapshot-read and subscribe where an event could be missed — that's tolerable because:

1. The snapshot includes all open orders + recent trades + halt state, so any event that fires "during" the snapshot is either:
   - Already reflected in the snapshot (the read is consistent at the time it ran), or
   - About to fire on the channel — but the client will get it via the next reconnect's snapshot if the worker missed it.
2. The window is sub-millisecond on a healthy stack — far below the human-perception threshold of UI staleness.

The "tail" task pair (heartbeat + forward) is coupled with `asyncio.wait(..., FIRST_COMPLETED)` (`api/websocket.py:395`) so when **either** completes (heartbeat exits on send-failure / cancellation; forward exits on disconnect or pubsub error), the other is cancelled. This prevents a leaked subscription on an idle channel where the forward loop would block forever.

### 3.2 Producer side — TradingNode → projection consumer → Redis pub/sub

```
 TradingNode subprocess    Projection consumer       Redis
 ┌──────────────────┐      ┌──────────────────┐      ┌──────────────┐
 │  Strategy fires  │      │                  │      │              │
 │  on_order_event  │      │                  │      │              │
 │  on_fill_event   │      │                  │      │              │
 │  on_account_     │      │                  │      │              │
 │  state_event     │      │                  │      │              │
 │                  │      │                  │      │              │
 │  ▼               │      │                  │      │              │
 │ MessageBus.publish( stream_name=           │      │              │
 │   dep.message_bus_stream )                 │      │              │
 │  ▼               │      │                  │      │              │
 │ XADD (Redis stream)  ──▶│ XREADGROUP       │      │  STREAM:     │
 │  (per-deployment        │                  │      │  dep.message │
 │   stream provisioned    │                  │      │  _bus_stream │
 │   by supervisor —       │                  │      │  (durable,   │
 │   value lives in        │                  │      │   PEL-       │
 │   LiveDeployment        │                  │      │   recoverable)│
 │   .message_bus_stream)  │ build Internal   │      │              │
 │                  │      │ Event (fill /    │      │              │
 │                  │      │ order_status /   │      │              │
 │                  │      │ position / acct /│      │              │
 │                  │      │ risk_halt /      │      │              │
 │                  │      │ deployment_      │      │              │
 │                  │      │ status)          │      │              │
 │                  │      │                  │      │              │
 │                  │      │ DualPublisher.publish(event)           │
 │                  │      │   ▼              │      │              │
 │                  │      │ redis.publish(   │      │              │
 │                  │      │  state_channel,  │ ───▶ │  PUB/SUB:    │
 │                  │      │  json)           │      │  msai:live:  │
 │                  │      │                  │      │  state:{id}  │
 │                  │      │ redis.publish(   │      │              │
 │                  │      │  events_channel, │ ───▶ │  PUB/SUB:    │
 │                  │      │  json)           │      │  msai:live:  │
 │                  │      │                  │      │  events:{id} │
 │                  │      │                  │      │              │
 │                  │      │ XACK (only after │      │   fan-out to │
 │                  │      │  BOTH publishes  │      │   N subs per │
 │                  │      │  succeed —       │      │   channel    │
 │                  │      │  failure → leave │      │              │
 │                  │      │  in PEL for      │      │              │
 │                  │      │  XAUTOCLAIM)     │      │              │
 │                  │      │                  │      │              │
 │                  │      │ on poison entry: │      │  STREAM:     │
 │                  │      │ XADD to DLQ ─────│ ───▶ │  msai:live:  │
 │                  │      │ (after retry     │      │  events:dlq: │
 │                  │      │  exhaustion)     │      │  {id}        │
 └──────────────────┘      └──────────────────┘      └──────────────┘
```

The TradingNode → projection consumer link is a Redis **Stream** (durable, consumer-group-based, with PEL recovery). The stream key is per-deployment — `LiveDeployment.message_bus_stream`, a value provisioned at portfolio start (`api/live.py:432` calls `derive_message_bus_stream(slug)`) and registered with the projection consumer (`api/live.py:573`, `main.py:170`). Don't grep for a literal `msai:live:trader:{tid}` — there isn't one; the stream name is whatever the supervisor wrote into that column at deployment-create time.

The projection consumer → subscribers link is Redis **pub/sub** on two channels (state + events; see §1.5). Each link's semantic matches its purpose:

- **Stream (TradingNode → consumer)** — durability matters. If the consumer dies and restarts, it picks up un-ACKed events from the PEL. No event lost. Repeated failures on the same entry land it in the DLQ stream `msai:live:events:dlq:{deployment_id}` (`consumer.py:68 DLQ_STREAM_PREFIX`) so a poison message can't wedge the consumer indefinitely.
- **Pub/sub (consumer → state/WS)** — durability **doesn't** matter. If a worker is down or no subscriber is connected, the message is discarded. That's fine — every worker's `StateApplier` started at boot is already subscribed to `msai:live:state:*`, and any client gap on the events channel is covered by the snapshot replay on reconnect.

This split is deliberate. If the WS path used streams too, every WS client would need a consumer ID, every disconnect would leave a stale PEL entry, and the fan-out wouldn't work without explicit XCLAIM / XAUTOCLAIM dance. Pub/sub is the right primitive for the read-path.

---

## 4. See / Verify / Troubleshoot

### 4.1 The `/dashboard` page

The dashboard at `:3300/dashboard` is the **portfolio-level summary** — it doesn't bind to a single deployment, doesn't open a WebSocket, and doesn't poll. The page runs **one fetch on mount** and renders four blocks. The deeper, WS-driven monitoring lives on `/live-trading` (§4.2) — the dashboard is just the at-a-glance landing surface.

What's actually on the page (`frontend/src/app/dashboard/page.tsx`):

- **`<PortfolioSummary>`** (`components/dashboard/portfolio-summary.tsx`) — four stat cards: **Total Value** (from `accountData.net_liquidation`), **Daily P&L** (from `accountData.unrealized_pnl`), **Total Return** (currently rendered as `--`, no data source wired), and **Active Strategies** (`runningCount / totalStrategies`).
- **`<EquityChart>`** (`components/dashboard/equity-chart.tsx`) — the dashboard invokes it with `data={[]}`, so it renders the "No equity data available yet." empty state. The component itself is implemented, but no source feeds it; treat the equity curve as **not yet wired**.
- **`<ActiveStrategies>`** — list view of deployments from `GET /api/v1/live/status`.
- **`<RecentTrades>`** — recent fills table.

Frontend wiring (`dashboard/page.tsx:29-68`):

- A single `useEffect` runs on mount and issues `Promise.allSettled` over three requests: `apiGet("/api/v1/strategies/")`, `getAccountSummary(token)`, `getLiveStatus(token)`.
- There is **no `setInterval`**, no 30 s polling loop, and no WebSocket subscription on this page. To get an updated view, the user reloads the page.
- An inline error banner renders at the top of the content area on fetch failure (`text-red-400` div around line 81). There is no toast/sonner/Toaster mounted anywhere in `frontend/src/`, so alert notifications surface only via `GET /api/v1/alerts/` if a caller polls it (§4.4) or via the `/live-trading` WebSocket if the operator is on that page.

What is **not** on the dashboard (despite being plausible features for a future iteration): a halt-flag banner, alert toasts, an IB-gateway health pill in the top nav, an IB-vs-MSAI position-divergence badge, and any kind of live-event-driven refresh. The kill switch lives only on `/live-trading` (`components/live/kill-switch.tsx`); the dashboard does not import or render it.

### 4.2 The `/live-trading` page

The `/live-trading` page is the **deployment detail monitor**. One deployment at a time, drill-down view. Sections:

- **Deployment list** — left rail, populated from `GET /api/v1/live/status`. Click a row to switch active deployment.
- **Strategy status** — the `<StrategyStatus>` component (`frontend/src/app/live-trading/page.tsx:199`) shows per-strategy state derived from the WS snapshot's `status` field plus subsequent `deployment_status` events.
- **Positions table** — `<PositionsTable>` (`page.tsx:200`) renders merged WS + REST data. The REST side (`getLivePositions`) is the fallback while the WebSocket is connecting; once `useLiveStream` is open, the WS-derived positions take precedence.
- **Kill switch** — `<KillSwitch>` (`page.tsx:139`) is the panic button. Wired to `POST /api/v1/live/kill-all` (covered in doc 7).
- **Token fetch** — `useAuth().getToken()` retrieves the JWT before WS connect. The token is passed as the WS first message (see `useLiveStream` hook at `frontend/src/lib/use-live-stream.ts`).

Frontend wiring summary:

- `live-trading/page.tsx:78` — `useLiveStream(activeRealDeployment?.id ?? null, { token })` opens the WebSocket.
- `use-live-stream.ts` — owns reconnect, snapshot caching, event dispatch, and cleanup.

### 4.3 Verifying a WebSocket is connected

Three independent checks, in increasing order of certainty:

**Browser DevTools.** Open Network tab → filter "WS" → click the row → Frames tab. You should see:

1. The first text frame is your JWT (sent from client).
2. The first received JSON is `{"type":"snapshot",...}`.
3. Periodic `{"type":"heartbeat","ts":"..."}` if the channel is idle, OR live `{"event_type":"...","..."}` payloads if the strategy is firing.

If frame #1 is missing — your client isn't sending the token. If frame #1 sends but no #2 arrives within 5s, the server closed (4001) — see § 5.1.

**Server logs.** The handler emits structured logs:

```
ws_authenticated         user=<sub>
ws_subscribed            channel=msai:live:events:<id>
ws_snapshot_emitted      deployment_id=<id> positions=<N> orders=<N> trades=<N>
                         has_account=<bool> has_status=<bool> halted=<bool>
```

Tail with `docker compose -f docker-compose.dev.yml logs -f api | grep ws_`.

**Redis pub/sub.** Verify the channel has subscribers:

```bash
docker compose -f docker-compose.dev.yml exec redis redis-cli PUBSUB CHANNELS 'msai:live:events:*'
docker compose -f docker-compose.dev.yml exec redis redis-cli PUBSUB NUMSUB msai:live:events:<id>
```

If the channel is in the list but `NUMSUB` is `0`, no worker is subscribed — your WS handler exited (probably on auth failure). If the channel is **not** in the list and you've connected a client, the subscribe step failed — check Redis connectivity from the FastAPI worker.

### 4.4 The alert audit endpoint

`GET /api/v1/alerts/` is the operator's read-only audit trail. Curl it directly:

```bash
curl -H "X-API-Key: $MSAI_API_KEY" \
  'http://localhost:8800/api/v1/alerts/?limit=20' | jq '.alerts[]'
```

Each entry has `type`, `level` (`info` / `warning` / `error` / `critical`), `title`, `message`, and `created_at` (`schemas/alert.py`). Use it post-incident — when an operator notices something went wrong, the alerts feed has the structured record with the originating message. The on-disk store is `{data_root}/alerts/alerts.json` — a single JSON document with an `alerts` array, **not** a JSONL log; `tail -f data/alerts.log` will find nothing.

---

## 5. Common Failures

### 5.1 JWT not received within 5s

**Symptom.** Browser opens WS, server logs `ws_auth_timeout`, server closes with code 4001, browser sees "WebSocket closed: 4001 Authentication timed out".

**Cause.** The client failed to send the first text message within `_AUTH_TIMEOUT_SECONDS`. Common reasons:

- Browser took longer than 5s to fetch a fresh token from the auth provider.
- A reverse proxy (nginx, Cloudflare) is buffering the WS handshake.
- Client code calls `ws.send(token)` before the `open` event fires, and the send is queued — but the open is delayed.
- Network latency to the auth provider was >5s on a cold cache.

**Fix.** The frontend hook (`use-live-stream.ts`) is supposed to call `ws.send(token)` from inside the `onopen` handler — never before. If you're writing a custom client, do the same. If you're behind a proxy that buffers, set `proxy_buffering off` for the WS path.

**Why not bump the timeout?** Because 5s is the deliberate budget — anything longer makes the system more vulnerable to slow-loris-style WS half-open attacks. The fix is in the client, not the server.

### 5.2 Reconnect storm

**Symptom.** Server logs show a flood of `ws_authenticated` / `ws_subscribed` / `ws_client_disconnected` per second, all from the same client IP. Browser CPU spikes; dashboard updates flicker.

**Cause.** Three known patterns:

- **Bad reconnect logic.** Client reconnects immediately on disconnect with no backoff. A transient blip becomes a tight loop.
- **Auth token expiry mid-loop.** The token expires, the WS disconnects on next event (auth check failed elsewhere or proxy timeout), the client reconnects, the new token is also expired (because the auth refresh hasn't run), the loop continues.
- **Unhealthy deployment.** The strategy is throwing on every event, the projection consumer is logging exceptions, and something upstream is causing the WS to close repeatedly.

**Mitigation.** The frontend hook implements exponential backoff (cap typically at 30s). The server doesn't currently rate-limit per-IP at the WS layer — if you need that, add it at the reverse proxy (nginx `limit_req` for the WS upgrade endpoint) or wrap the `/api/v1/live/stream/...` route with a `slowapi` rate limiter on the auth handshake step.

**Operator action.** When you see a storm, stop the deployment (`POST /api/v1/live/stop`) before debugging. A spammy WS is downstream of a flooded event source; the cure is at the source.

### 5.3 Halt-flag set but client confused

**Symptom.** The `<KillSwitch>` on `/live-trading` shows a halt for a deployment, or the WS keeps streaming `risk_halt` events with `halted: true`, after the operator believed they had cleared the halt. Or: a fresh `POST /api/v1/live/start-portfolio` returns immediately with a halt rejection.

**Cause.** Halt-flag state lives in two places, and they have different lifecycles:

- **Per-deployment halt** — recorded as a `risk_halt` event in `ProjectionState`. Cleared when the engine sees a `cleared` event or the deployment ends.
- **Global halt flag** — Redis key `msai:risk:halt`, 24-hour TTL. Set by `POST /api/v1/live/kill-all`. Survives subprocess restarts. Cleared explicitly via `POST /api/v1/live/resume`.

The live-trading view treats a deployment as halted if **either** signal is active. To clear the per-deployment halt, the strategy's risk overlay needs to fire its "cleared" event (or the deployment is stopped, in which case the indicator goes away when the deployment leaves the running list). To clear the global flag, call `POST /api/v1/live/resume` — covered in doc 7.

### 5.4 Redis pub/sub broker hiccup

**Symptom.** A handful of events show up in the audit trail (Postgres `Trade` table, `OrderAttemptAudit` table) but never appeared in the live-trading WS stream. Or: WS connections briefly drop but reconnect cleanly.

**Cause.** Redis pub/sub is **not durable**. If Redis restarts, every subscriber's subscription is dropped; if a publish happens with no subscribers, the message is discarded. Network partitions between the projection consumer and Redis lose any in-flight publishes. This is by design — pub/sub is the read-path, where ephemeral loss is acceptable because the Postgres-backed snapshot recovers state on reconnect.

**Recovery.** **The next reconnect's snapshot is your recovery mechanism.** When the client reconnects:

- The snapshot reads from Postgres (`OrderAttemptAudit`, `Trade`) and from `ProjectionState`.
- Postgres has the durable record of every order attempt and every fill — those weren't on Redis pub/sub.
- `ProjectionState` is in-memory but rebuilt from the Redis Stream (`XREADGROUP`-replayable) on consumer restart, so it has the up-to-date positions / account / status / halt state.

So a Redis pub/sub blip means a few seconds of "live" event invisibility, fully recovered on next reconnect. **No event is permanently lost** — just temporarily not-pushed. If `/live-trading` shows a stale view and you suspect a hiccup, **force-reload the page** (Cmd-R / Ctrl-R) — that triggers a fresh WS connect and a fresh snapshot.

### 5.5 IB Gateway probe always reports `gateway_connected=false`

**Symptom.** The health badge is always red. `/account/health` returns `{"status": "unhealthy", "gateway_connected": false, "consecutive_failures": "0"}`.

**Cause.** The probe task (`account.py:46` `start_ib_probe_task`) wasn't started. The drill on 2026-04-15 hit this three times in a row:

- The probe is created at module import (`account.py:32`).
- The periodic loop is started by FastAPI's lifespan handler.
- If the lifespan didn't run (test mode, weird import order, manual app construction), the probe never calls `check_health`, and `_is_healthy` stays at its initial `False`.

**Fix.** Verify the probe task is running:

```bash
docker compose -f docker-compose.dev.yml exec api python -c \
  "from msai.api.account import _probe_task; print(_probe_task)"
```

Should be a `Task` object with `done() == False`. If it's `None`, the lifespan startup didn't call `start_ib_probe_task`. Restart the FastAPI worker (`docker compose -f docker-compose.dev.yml restart api`).

### 5.6 Unauthorized — closes with 4403

**Symptom.** WebSocket closes immediately after auth with code 4403 ("Forbidden"). Logs show `ws_authorization_denied`.

**Cause.** The JWT user resolved from the token doesn't match `LiveDeployment.started_by` for the requested deployment. This is the per-user authorization check (`api/websocket.py:279`). Two specific subcases:

- The deployment was started by user A, user B is trying to subscribe. Single-tenant local mode (API key) bypasses this; multi-user JWT mode enforces it.
- The deployment row has `started_by = NULL` (legacy row). The check denies by default rather than failing open.

**Fix.** For local dev, use `MSAI_API_KEY` as `X-API-Key` header (or as the WS first message) — the API-key dev account (`sub == "api-key-user"`) sees every deployment. For production multi-user setups, the user fetching the dashboard must be the same user that started the deployment.

---

## 6. Idempotency and Retry Behavior

Real-time monitoring is **read-only**. Idempotency is therefore trivial:

- **Reconnects are unlimited and cheap.** Every reconnect runs the snapshot read (Postgres + ProjectionState) and re-subscribes to the pub/sub channel. No write happens. No state is mutated. The same snapshot read at the same instant produces the same JSON payload.
- **REST endpoints are pure GETs.** `/account/summary`, `/account/portfolio`, `/account/health`, `/live/status`, `/live/positions`, `/live/trades`, `/alerts/` — all GET, all idempotent. No body, no side effect.
- **Hydration is replay-safe.** The snapshot reads from `OrderAttemptAudit`, `Trade`, and `ProjectionState`. None of these are mutated by the read. Replaying the snapshot for a different (or the same) client ten times in a row is exactly equivalent to replaying it once.

There is **one** thing to be careful about: the `OrderAttemptAudit` and `Trade` tables grow without bound. The hydration query uses `limit=50` for trades and a "still-open" filter for orders, so the snapshot stays bounded. But on a deployment that's been running for months, the per-deployment row count can be millions. The query has indexes on `(deployment_id, created_at DESC)` for both tables — verify those indexes are intact if hydration suddenly gets slow.

**Retry semantics for the client.** The frontend hook treats:

- **Closing-with-4001 (auth failure)** — terminal. Don't retry. The user needs a fresh token.
- **Closing-with-4403 (forbidden)** — terminal. Don't retry. The user isn't allowed to see this deployment.
- **Closing-with-4404 (deployment not found)** — terminal. The deployment was deleted; reload the deployment list.
- **Closing-with-1011 (snapshot failed)** — retry with backoff. Server-side hiccup.
- **Closing-with-1006 / network error** — retry with exponential backoff (1s, 2s, 4s, …, cap 30s).
- **Heartbeat gap >60s** — assume dead socket, force-reconnect.

---

## 7. Rollback / Repair

There is **no rollback** for monitoring — it's a read-only surface. Reverting a "monitoring change" means deploying a different frontend bundle or backend WS handler. Nothing about the live deployment itself changes.

**Repair flows:**

- **WS stuck (no events arriving, snapshot was sent successfully).**
  Restart the FastAPI worker:

  ```bash
  docker compose -f docker-compose.dev.yml restart api
  ```

  Reload the dashboard. New WS connect + new snapshot.

- **Projection consumer stuck (no events being published to Redis).**
  Restart the live-supervisor + workers:

  ```bash
  ./scripts/restart-workers.sh --with-broker
  ```

  The projection consumer is part of the worker stack; restarting picks up un-ACKed entries from the Redis Stream PEL via `XAUTOCLAIM`.

- **Halt banner stuck on after the deployment has stopped.**
  Check `msai:risk:halt` (24h TTL):

  ```bash
  docker compose -f docker-compose.dev.yml exec redis redis-cli GET msai:risk:halt
  ```

  Clear via API:

  ```bash
  curl -X POST -H "X-API-Key: $MSAI_API_KEY" \
    http://localhost:8800/api/v1/live/resume
  ```

- **IB health badge red but TWS shows the gateway is up.**
  Restart the API worker (resets the probe task) — see § 5.5.

- **`GET /api/v1/alerts/` returns `[]` despite alerts being emitted.**
  The alerts feed is file-backed at `{data_root}/alerts/alerts.json` (one JSON document with an `alerts` array — **not** a `.log` file, **not** JSONL). Inspect it directly:

  ```bash
  docker compose -f docker-compose.dev.yml exec api \
    cat /app/data/alerts/alerts.json | jq '.alerts | length'
  ```

  If the file has entries but the API returns `[]`, the bounded read fired (`_HISTORY_WRITE_TIMEOUT_S = 2.0`) — usually means another writer is holding `flock`. Wait or restart the offending writer.

**The kill switch.** The 4-layer kill-all (`POST /live/kill-all`) is documented in doc 7 — this doc shows where you'd see the halt take effect:

- **`<KillSwitch>` on `/live-trading`** (`components/live/kill-switch.tsx`) updates as the WS streams the `risk_halt` event for the active deployment. There is no equivalent halt banner on `/dashboard` today.
- **Halted deployment cards** in the live-trading list flip status to "halted" / "stopping" / "stopped" as the supervisor processes the kill — visible via the WS `deployment_status` events.
- **Alerts feed** records the halt as a structured entry (`type=risk_halt`, `level=critical`) under `GET /api/v1/alerts/`. The frontend does not yet pop this as a toast — it surfaces only via direct curl or future polling.
- **IB position list** (`/account/portfolio`) does **not** auto-flatten — Nautilus gotcha #13: stopping the trading node does not close positions. Operator must explicitly flatten via `<KillSwitch>` (4-layer) or manually in TWS.

---

## 8. Key Files

| Subsystem                           | Path                                                                                                                                         |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| WebSocket route registration        | `backend/src/msai/main.py:284` — `@app.websocket("/api/v1/live/stream/{deployment_id}")`                                                     |
| WebSocket handler                   | `backend/src/msai/api/websocket.py:313` — `live_stream(websocket, deployment_id)`                                                            |
| First-message auth                  | `backend/src/msai/api/websocket.py:90` — `_authenticate(websocket)`                                                                          |
| Auth-timeout constant               | `backend/src/msai/api/websocket.py:75` — `_AUTH_TIMEOUT_SECONDS = 5.0`                                                                       |
| Heartbeat interval constant         | `backend/src/msai/api/websocket.py:79` — `_HEARTBEAT_INTERVAL_SECONDS = 30.0`                                                                |
| Pub/sub poll timeout                | `backend/src/msai/api/websocket.py:85` — `_PUBSUB_GET_TIMEOUT_SECONDS = 1.0`                                                                 |
| Initial snapshot builder            | `backend/src/msai/api/websocket.py:118` — `_send_initial_snapshot`                                                                           |
| Heartbeat task                      | `backend/src/msai/api/websocket.py:201` — `_heartbeat_loop`                                                                                  |
| Pub/sub forward task                | `backend/src/msai/api/websocket.py:221` — `_forward_pubsub_to_websocket`                                                                     |
| Authorization check                 | `backend/src/msai/api/websocket.py:279` — `_is_authorized`                                                                                   |
| Reconnect-readers (orders/trades)   | `backend/src/msai/services/nautilus/projection/reconnect_reader.py` — `load_open_orders_for_deployment`, `load_recent_trades_for_deployment` |
| Dual-channel pub/sub publisher      | `backend/src/msai/services/nautilus/projection/fanout.py:62` — `DualPublisher.publish` (state + events)                                      |
| State channel naming                | `backend/src/msai/services/nautilus/projection/fanout.py:42` — `STATE_CHANNEL_PREFIX = "msai:live:state:"`                                   |
| Events channel naming               | `backend/src/msai/services/nautilus/projection/fanout.py:46` — `EVENTS_CHANNEL_PREFIX = "msai:live:events:"`                                 |
| `state_channel_for(deployment_id)`  | `backend/src/msai/services/nautilus/projection/fanout.py:52`                                                                                 |
| `events_channel_for(deployment_id)` | `backend/src/msai/services/nautilus/projection/fanout.py:57`                                                                                 |
| State applier (per-worker)          | `backend/src/msai/services/nautilus/projection/state_applier.py:71` — `PSUBSCRIBE msai:live:state:*`                                         |
| DLQ stream prefix                   | `backend/src/msai/services/nautilus/projection/consumer.py:68` — `DLQ_STREAM_PREFIX = "msai:live:events:dlq:"`                               |
| Account API                         | `backend/src/msai/api/account.py` — summary, portfolio, health                                                                               |
| `IBProbe` task lifecycle            | `backend/src/msai/api/account.py:46` — `start_ib_probe_task`                                                                                 |
| Probe interval                      | `backend/src/msai/api/account.py:43` — `_PROBE_INTERVAL_S = 30`                                                                              |
| Alerts API                          | `backend/src/msai/api/alerts.py:32` — `GET /api/v1/alerts/`                                                                                  |
| Alerts service (file-backed)        | `backend/src/msai/services/alerting.py` — `alerting_service`, `_HISTORY_EXECUTOR`                                                            |
| Frontend WS hook                    | `frontend/src/lib/use-live-stream.ts` — `useLiveStream(deployment_id, {token})`                                                              |
| Frontend dashboard page             | `frontend/src/app/dashboard/page.tsx` — account summary + live status                                                                        |
| Frontend live-trading page          | `frontend/src/app/live-trading/page.tsx` — deployment list + WS subscriber + kill switch                                                     |
| `<KillSwitch>` component            | `frontend/src/components/live/kill-switch.tsx`                                                                                               |
| `<StrategyStatus>` component        | `frontend/src/components/live/strategy-status.tsx`                                                                                           |
| `<PositionsTable>` component        | `frontend/src/components/live/positions-table.tsx`                                                                                           |
| Alert schemas                       | `backend/src/msai/schemas/alert.py` — `AlertRecord`, `AlertListResponse`                                                                     |

---

## Cross-references

- **Previous doc:** [How Live Portfolios and IB Accounts Work →](how-live-portfolios-and-ib-accounts.md) — sets up everything this doc shows you watching.
- **Back to the journey:** [The Developer Journey →](00-developer-journey.md) — the front-of-house orientation that frames where this doc sits in the larger picture.
- **Nautilus gotcha #13:** Stopping a TradingNode does **not** close positions. The `<KillSwitch>` state on `/live-trading` (and the `risk_halt` event on the WS) is the cue to manually flatten via TWS or the 4-layer kill-all flow.
- **Nautilus gotcha #19:** On restart, the engine reconciles against IB and may discover fills that landed after the last save. These appear as a flood of `fill` events on the WS shortly after a deployment restart — that's normal, not a duplicate-trade bug.
- **Alert lifecycle** is owned by `services/alerting.py`. The audit log is opportunistic — for guaranteed delivery, watch the WS event stream for `risk_halt` and `deployment_status` instead of polling `/alerts/`.

---

**Date verified against codebase:** 2026-04-28
**This is the last document in the Developer Journey.** Loop back to [00-developer-journey.md](00-developer-journey.md) for the front-of-house orientation, or reach for any of the eight subsystem docs by name.
