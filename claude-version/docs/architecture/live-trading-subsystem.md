# Live Trading Subsystem

Deep dive on the supervisor, subprocess lifecycle, heartbeat monitoring,
startup watchdog, and the four-layer kill switch.

## Architecture Overview

```
+---------------------------------------------------------------+
|  live-supervisor container                                    |
|  (python -m msai.live_supervisor)                             |
|                                                               |
|  +-------------------+  +----------------+  +---------------+ |
|  | LiveCommandBus    |  | ProcessManager |  | Heartbeat     | |
|  | (Redis Streams    |  | (spawn, stop,  |  | Monitor       | |
|  |  XREADGROUP +     |  |  reap_loop,    |  | (stale sweep  | |
|  |  XAUTOCLAIM +     |  |  watchdog_loop)|  |  for post-    | |
|  |  DLQ)             |  |                |  |  startup rows)| |
|  +--------+----------+  +-------+--------+  +-------+-------+ |
|           |                      |                    |        |
|           v                      v                    |        |
|    msai:live:commands      mp.Process(target=         |        |
|    (Redis Stream)          _trading_node_subprocess)  |        |
|                                  |                    |        |
+----------------------------------+--------------------+--------+
                                   |
                     +-------------+-------------+
                     |  Trading Subprocess       |
                     |  (fresh Python interpreter)|
                     |                           |
                     |  TradingNodePayload       |
                     |  -> TradingNode(config)   |
                     |  -> node.build()          |
                     |  -> node.run_async()      |
                     |  -> wait_until_ready()    |
                     |  -> IBDisconnectHandler   |
                     |  -> _HeartbeatThread      |
                     |                           |
                     |  Connects to:             |
                     |    ib-gateway:4002 (paper) |
                     |    postgres:5432          |
                     |    redis:6379             |
                     +---------------------------+
```

## LiveCommandBus

Source: `services/live_command_bus.py`

The command bus is the control plane between FastAPI and the supervisor.
It uses Redis Streams with consumer groups.

| Constant                  | Value                    | Purpose                |
| ------------------------- | ------------------------ | ---------------------- |
| `LIVE_COMMAND_STREAM`     | `msai:live:commands`     | Primary command stream |
| `LIVE_COMMAND_GROUP`      | `live-supervisor`        | Consumer group name    |
| `LIVE_COMMAND_DLQ_STREAM` | `msai:live:commands:dlq` | Dead letter queue      |
| `MAX_DELIVERY_ATTEMPTS`   | 5                        | Max retries before DLQ |

Command types (`LiveCommandType` enum):

- `START` -- deploy a strategy
- `STOP` -- stop a deployment

Recovery semantics:

- On startup and at `recovery_interval_s`, the bus runs `XAUTOCLAIM`
  to pick up entries stuck in the PEL from a crashed supervisor.
- Entries exceeding `MAX_DELIVERY_ATTEMPTS` are moved to the DLQ with
  `original_entry_id`, `delivery_count`, `dlq_reason`, and `moved_at`
  metadata.
- The supervisor's `run_forever` loop only calls `bus.ack(entry_id)`
  when the handler returns `True`. A `False` return or exception
  leaves the entry in the PEL for retry. Malformed commands (unknown
  type) are ACKed immediately to prevent infinite bounce.

## ProcessManager

Source: `live_supervisor/process_manager.py`

### The INSERT-spawn-UPDATE Pattern (3-Phase)

The spawn flow is split into three phases, each in its own transaction,
to prevent the race condition where `process.start()` succeeds but the
DB commit fails, leaving a live subprocess with no committed row.

**Phase A -- Reserve the slot** (one transaction):

1. `SELECT FOR UPDATE` the `live_deployments` row by `deployment_slug`
2. Check for existing active `live_node_processes` row
   - Status `stopping` -> return `BUSY_STOPPING` (no ACK, retry later)
   - Any other active status -> return `ALREADY_ACTIVE` (ACK, idempotent)
3. `INSERT` new `live_node_processes` row with `status='starting'`, `pid=None`
4. `COMMIT`
5. If INSERT races a concurrent spawn -> `ALREADY_ACTIVE` (partial unique index)

**Phase B -- Halt-flag re-check + spawn** (NO db transaction):

1. `EXISTS msai:risk:halt` in Redis -> if set, mark failed with `HALT_ACTIVE`, ACK
2. Call `payload_factory(row_id, deployment_id, deployment_slug, payload_dict)`
   - Permanent errors (ValueError, ImportError, etc.) -> `SPAWN_FAILED_PERMANENT`, ACK
   - Transient errors (OperationalError, network) -> `SPAWN_FAILED_TRANSIENT`, no ACK
3. Second halt-flag re-check (closes race during payload factory await)
4. `mp.Process(target=spawn_target, args=spawn_args).start()`
   - On failure -> `SPAWN_FAILED_PERMANENT`, ACK
5. Store handle in `self.handles[deployment_id]`

**Phase C -- Record the pid** (one transaction):

1. `UPDATE live_node_processes SET pid = process.pid`
2. On failure, log but continue (subprocess self-write is the backup)

### Production Payload Factory

Source: `live_supervisor/__main__.py:_build_production_payload_factory`

The factory reads the `live_deployments` row (joined with `strategies`)
and constructs a `TradingNodePayload` with:

- `strategy_path` + `strategy_config_path` resolved via `resolve_importable_strategy_paths`
- `paper_symbols` extracted from `deployment.instruments` (strip venue suffix)
- `ib_host` / `ib_port` from process-wide settings
- `ib_account_id` from `deployment.account_id` (not settings default)
- `database_url` / `redis_url` from settings
- Strategy config with `instrument_id` and `bar_type` defaults derived from instruments

Safety validations:

- Paper/live port consistency: `paper_trading=True` requires `IB_PORT=4002`
- Account prefix consistency: paper accounts must start with `DU`
- Mismatch raises `ValueError` -> `SPAWN_FAILED_PERMANENT`

### Reap Loop

Source: `process_manager.py:reap_once`, `reap_loop`

Polls `self.handles` every 1 second. For any `is_alive() == False` child:

1. `proc.join(timeout=1)`
2. `_on_child_exit(deployment_id, proc.exitcode)`
3. Remove from handles map

Exit code mapping in `_on_child_exit`:

- `0` -> status=`stopped`, failure_kind=`NONE`
- `2` -> status=`failed`, failure_kind=`RECONCILIATION_FAILED`
- Other (1, None, etc.) -> status=`failed`, failure_kind=`SPAWN_FAILED_PERMANENT`

The method only backfills `failure_kind` if it's still `NULL`, so it
never overwrites a richer diagnosis the subprocess already persisted.

### Startup Watchdog

Source: `process_manager.py:watchdog_once`, `watchdog_loop`

Runs every `_watchdog_poll_interval_s` (default 30s). Scans for
`starting` or `building` rows whose `started_at` exceeds
`_startup_hard_timeout_s` (default 1800s = 30 minutes) AND whose `host`
matches this supervisor's hostname.

For each stale row:

1. SIGKILL the pid (handle map first, fallback to `row.pid`)
2. Mark the row `failed` / `BUILD_TIMEOUT`

Why this exists: the heartbeat thread starts BEFORE `node.build()`, so
a wedged build keeps `last_heartbeat_at` fresh forever.
`HeartbeatMonitor` deliberately excludes `starting`/`building` from its
stale sweep. Without this watchdog, a wedged subprocess would hold the
active-row unique-index slot indefinitely and block every future
`/start` for that deployment.

Hostname scoping prevents a multi-supervisor deployment from killing
PIDs that belong to another host's PID namespace.

### Stop

Source: `process_manager.py:stop`

1. Find the latest active `live_node_processes` row
2. Cross-host guard: if `row.host != socket.gethostname()`, return
   `False` (no ACK, XAUTOCLAIM redeliver to correct supervisor)
3. Flip row to `status='stopping'`
4. `os.kill(pid, SIGTERM)` -- handle map first, fallback to `row.pid`
5. The reap loop observes the exit on its next pass

## HeartbeatMonitor

Source: `live_supervisor/heartbeat_monitor.py`

The heartbeat monitor is the SOLE authority for post-startup rows
(`ready`, `running`, `stopping`). It never touches `starting` or
`building` rows -- those belong to the watchdog.

Configuration:

- `stale_seconds`: 30 (default) -- rows older than this are dead
- `sleep_interval_s`: 10.0 (default) -- sweep frequency

Sweep query:

```sql
UPDATE live_node_processes
SET status = 'failed',
    error_message = 'heartbeat timeout',
    failure_kind = 'unknown'
WHERE status IN ('ready', 'running', 'stopping')
  AND last_heartbeat_at < (now() - interval '30 seconds')
RETURNING deployment_id
```

## Trading Subprocess Lifecycle

Source: `services/nautilus/trading_node_subprocess.py`

Entry point: `_trading_node_subprocess(payload: TradingNodePayload)`

The subprocess runs in a fresh Python interpreter under
`mp.get_context('spawn')`. It owns one NautilusTrader `TradingNode`
from construction through shutdown.

### Lifecycle Steps (run_subprocess_async)

```
1. Self-write pid + status='building'
2. Start _HeartbeatThread (BEFORE node.build)
3. Shutdown checkpoint (early SIGTERM check)
4. node = node_factory(payload)
5. node.build()  (IB contract loading, synchronous)
6. Shutdown checkpoint (post-build SIGTERM check)
7. node_run_task = asyncio.create_task(node.run_async())
8. wait_until_ready(node, timeout_s)
   - Polls node.kernel.trader.is_running
   - Raises StartupHealthCheckFailed on timeout
9. Check if run_async task already crashed
10. Shutdown checkpoint
11. status='ready', then status='running'
12. Start IBDisconnectHandler as sibling task (if configured)
13. await node_run_task (blocks until SIGTERM or engine failure)
14. FINALLY:
    a. Cancel disconnect handler task
    b. heartbeat.stop()
    c. node.stop_async() (idempotent)
    d. node.dispose() (handled outside asyncio.run in production)
    e. _mark_terminal() -- write final status to DB
```

### Exit Codes

| Code | Meaning                                     | FailureKind            |
| ---- | ------------------------------------------- | ---------------------- |
| 0    | Clean stop (SIGTERM or normal shutdown)     | NONE                   |
| 1    | Generic exception (build/start/run failure) | SPAWN_FAILED_PERMANENT |
| 2    | Startup health check timed out              | RECONCILIATION_FAILED  |

### HeartbeatThread

Source: `trading_node_subprocess.py:_HeartbeatThread`

A daemon thread with its own asyncio loop and its own `AsyncEngine`.
Bumps `live_node_processes.last_heartbeat_at` every 5 seconds.

- Starts BEFORE `node.build()` (decision #17)
- Stops in `finally` block AFTER cleanup
- Never lets a transient DB blip kill the loop
- `_stop_event` is polled in 0.1s steps for responsive shutdown

### SIGTERM Handling

When `install_signal_handlers=True` (production), the subprocess
registers async-aware SIGTERM/SIGINT handlers via
`loop.add_signal_handler`. On signal:

1. Set `shutdown_requested` event
2. Schedule `node.stop_async()` as a task
3. `node.run_async()` falls out of its `asyncio.gather` and returns
4. The finally block runs cleanup

During `node.build()` (synchronous, blocks the loop), SIGTERM lands but
is only processed after build returns. The post-build shutdown checkpoint
catches it. The supervisor's watchdog is the external backstop for truly
wedged builds.

### Dispose Caveat

Nautilus 1.223.0 `TradingNode.dispose()` calls `loop.stop()` if the
kernel's loop is running. Since `asyncio.run` owns the loop, this breaks
with `RuntimeError: Event loop stopped before Future completed`. The
production wrapper passes `skip_dispose=True` to `run_subprocess_async`
and calls `node.dispose()` AFTER `asyncio.run` returns.

## Four-Layer Kill Switch

Source: `api/live.py:live_kill_all` (POST /api/v1/live/kill-all)

The kill switch has four defense-in-depth layers:

### Layer 1: Persistent Halt Flag (API Endpoint)

Sets `msai:risk:halt` in Redis with a 24h TTL. Also sets
`msai:risk:halt:set_by` and `msai:risk:halt:set_at` metadata keys.

Every `POST /api/v1/live/start` checks this flag at the top of the
handler and returns 503 if set. Blocks any NEW deployments.

### Layer 2: Supervisor-Side Halt Re-Check (ProcessManager.spawn)

The supervisor re-checks `msai:risk:halt` AFTER reserving the DB slot
but BEFORE `process.start()`. This catches:

- Commands queued in `msai:live:commands` before the kill-all
- Commands reclaimed from the PEL via XAUTOCLAIM

A second re-check runs after the payload factory await to close the
race window during slow payload construction.

### Layer 3: Push-Based Stop (API Endpoint)

For every `live_node_processes` row with status in
`(starting, building, ready, running)`, the endpoint publishes a STOP
command via `LiveCommandBus`. The supervisor then SIGTERMs the
subprocess. Nautilus's `manage_stop=True` flatten loop closes positions.

Latency from `/kill-all` to flatten: < 5 seconds in normal operation.

If any stop command fails to publish, the endpoint returns HTTP 207
(Multi-Status) with a partial success body and logs CRITICAL.

### Layer 4: In-Strategy Halt-Flag Check (RiskAwareStrategy Mixin)

The `RiskAwareStrategy` mixin (in `services/nautilus/risk/`) checks
the halt flag before every order submission. Refuses any new orders
the strategy might emit between SIGTERM and the subprocess actually
exiting.

### Kill-All Response

| HTTP Status | Meaning                                                 |
| ----------- | ------------------------------------------------------- |
| 200         | All active deployments stopped successfully             |
| 207         | Partial success -- some stop commands failed to publish |

### Resume

`POST /api/v1/live/resume` clears the halt flag. There is intentionally
NO auto-resume. The operator must explicitly unblock before `/start`
will accept new deployments again. Each deployment must be re-started
individually.

## IB Disconnect Handler

Source: `services/nautilus/disconnect_handler.py`

A background task running INSIDE the trading subprocess that watches
the IB connection state.

Configuration:

- `DEFAULT_GRACE_SECONDS`: 120.0 (2 minutes)
- `DEFAULT_POLL_INTERVAL_S`: 1.0
- `_HALT_TTL_SECONDS`: 86400 (24 hours)
- `_HALT_SET_MAX_ATTEMPTS`: 5 (with exponential backoff starting at 100ms)

Flow:

1. On first disconnect, start a timer
2. If IB reconnects within 120s, cancel timer, log transient disconnect
3. If grace window expires while still disconnected:
   a. Set `msai:risk:halt` in Redis (with retries)
   b. Set `msai:risk:halt:reason` = `ib_disconnect`
   c. Set `msai:risk:halt:source` = `ib_disconnect_handler:{deployment_slug}`
   d. Best-effort email alert via AlertService
   e. Call `on_halt` callback (sets local `shutdown_requested` + schedules `node.stop_async()`)
4. Stay halted until operator calls `/api/v1/live/resume`

There is NO auto-resume on reconnect. Even after a clean reconnect,
the platform stays halted until the operator manually reviews and
resumes.

## Status State Machine

The `live_node_processes.status` column transitions:

```
starting -> building -> ready -> running -> stopping -> stopped
    |           |         |        |           |
    +-----+-----+---------+--------+-----------+
          |
          v
        failed
```

| Status   | Written By      | Authority For Cleanup |
| -------- | --------------- | --------------------- |
| starting | ProcessManager  | Startup Watchdog      |
| building | Subprocess      | Startup Watchdog      |
| ready    | Subprocess      | HeartbeatMonitor      |
| running  | Subprocess      | HeartbeatMonitor      |
| stopping | ProcessManager  | HeartbeatMonitor      |
| stopped  | Subprocess/Reap | Terminal              |
| failed   | Any             | Terminal              |

## FailureKind Enum

Source: `services/live/failure_kind.py`

| Value                    | Meaning                                                   |
| ------------------------ | --------------------------------------------------------- |
| `NONE`                   | Clean exit (exit code 0)                                  |
| `HALT_ACTIVE`            | Blocked by kill switch halt flag                          |
| `SPAWN_FAILED_PERMANENT` | Operator config error (bad strategy path, import error)   |
| `SPAWN_FAILED_TRANSIENT` | Transient failure (DB blip, network timeout) -- retryable |
| `RECONCILIATION_FAILED`  | Startup health check timed out (exit code 2)              |
| `BUILD_TIMEOUT`          | Watchdog killed a wedged starting/building row            |
| `UNKNOWN`                | HeartbeatMonitor stale sweep, or unclassified             |
