# Redis flatness protocol — deployment-scoped broker-flat verification at `/stop`

**Status:** Active (introduced 2026-05-13 in `fix/stop-flatness-verification`,
Bug #2 of the live-deploy-safety-trio).

**Context.** Before this PR, `POST /api/v1/live/stop` returned 200 as soon
as the supervisor SIGTERMed the trading subprocess. The 2026-05-13
paper-money drill discovered that Nautilus's `Strategy.stop()` →
`market_exit()` loop can hit `max_attempts` and leave residual
positions when IB rejects the exit order (e.g. TIF preset mismatch).
The API claimed success while the broker still held shares.

**Constraint.** Account-level IB queries (`IBAccountService.get_portfolio()`)
expose only net positions by symbol — no deployment_id metadata —
so the API cannot determine "this deployment's positions are flat"
from the broker view alone. The authoritative source is the live
Nautilus subprocess's `kernel.cache.positions_open()`, filtered by
the deployment's member `strategy_id_full` set.

**Decision.** Three Redis keys carry a child→API report of
deployment-scoped flatness at shutdown:

| Key                                | Set by                      | Read by                                          | TTL   | Purpose                                                                                                                                                         |
| ---------------------------------- | --------------------------- | ------------------------------------------------ | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inflight_stop:{deployment_id}`    | API (SET NX EX)             | API                                              | 60 s  | Coalescing primitive: concurrent /stop callers SET-NX their fresh nonce; whoever wins is the originator, others read the existing value and poll on it.         |
| `flatness_pending:{deployment_id}` | Supervisor (RPUSH + EXPIRE) | Child subprocess (LPOP loop in shutdown-finally) | 120 s | Per-request "ticket" carrying `{stop_nonce, member_strategy_id_fulls}`. List so concurrent stops queue cleanly. Bounded at 32 entries via LTRIM.                |
| `stop_report:{stop_nonce}`         | Child (SET EX)              | API (GET poll)                                   | 120 s | The actual report: `{stop_nonce, deployment_id, broker_flat, remaining_positions, reason, reported_at}`. Per-nonce key — no consumer-group load-balancing risk. |

**Flow.**

```
                          ┌─────────────────────────────────┐
                          │ API POST /api/v1/live/stop      │
                          └─────────────────────────────────┘
                                          │
                                          ▼
              SET inflight_stop:{deployment_id} <nonce> NX EX 60
                                          │
                          ┌───────────────┴───────────────┐
                          │ acquired                       │ collision
                          ▼                                ▼
              publish STOP_AND_REPORT_FLATNESS    GET inflight_stop:{deployment_id}
              {stop_nonce, member_strategy_id_fulls}     │
                          │                              ▼
                          ▼                       (use existing nonce)
              ┌─────────────────────────────┐           │
              │ Supervisor consume          │           │
              └─────────────────────────────┘           │
                          │                              │
                          ▼                              │
              RPUSH flatness_pending:{deployment_id}     │
              + EXPIRE 120s + LTRIM -32 -1               │
                          │                              │
                          ▼                              │
              ProcessManager.stop() → SIGTERM child      │
                          │                              │
                          ▼                              │
              ┌─────────────────────────────┐            │
              │ Child shutdown-finally hook │            │
              └─────────────────────────────┘            │
                          │                              │
                          ▼                              │
              await node.stop_async()  (Nautilus market_exit runs)
                          │                              │
                          ▼                              │
              async with wait_for(5.0):                  │
                LPOP flatness_pending:{deployment_id} loop
                  for each ticket:                       │
                    read kernel.cache.positions_open()   │
                    filter by member_strategy_id_fulls   │
                    SET stop_report:{stop_nonce} <json> EX 120
                          │                              │
                          ▼                              ▼
                          └────────────────► API GET stop_report:{nonce} (poll)
                                             with exponential backoff
                                             50ms→100ms→200ms→400ms→800ms→1600ms
                                             deadline 45s (/stop) / 35s (/kill-all)
```

**Why these specific timeouts.** The command bus uses XAUTOCLAIM with
a `min_idle_ms` of 30 s (see `services/live_command_bus.py`). If a
STOP_AND_REPORT_FLATNESS command is consumed by the wrong supervisor
host (Phase 2 multi-supervisor topology) or by a host that's busy
restarting, the command waits 30 s before XAUTOCLAIM redelivers it
to a healthy consumer. The API poll deadlines must be longer than
that window so the report can still arrive:

- `/stop` 45 s: 30 s XAUTOCLAIM + 5 s redelivery + 10 s for the child
  to drain its cache and write the report.
- `/kill-all` 35 s: 30 s XAUTOCLAIM + 5 s buffer. Tighter than /stop
  because panic-button callers need a fast answer; if the report
  doesn't arrive, the response already says `any_non_flat: true` /
  `broker_flat: null` and the operator goes to IB portal anyway.

Phase 1 deployment is single-supervisor so XAUTOCLAIM never actually
fires for cross-host routing — every command lands on the only
consumer. The headroom is for restart-during-stop scenarios and
Phase 2 readiness. PR #65 Codex P2 round-6 flagged this.

**TTL rationale.**

- `inflight_stop` 60 s: must be ≥ the longest poll deadline (45 s) +
  slack. Lets a second caller arriving 25 s into the first's wait
  still observe the in-flight nonce.
- `flatness_pending` 120 s: long enough to outlive a slow Nautilus
  shutdown (IB socket teardown + Rust logger flush + `dispose()`
  observed at ~10-30 s on a healthy paper drill). Short enough that
  if the child never drains it, the list disappears before becoming
  a memory liability.
- `stop_report` 120 s: matches the child's worst-case
  pending-drain horizon. API only waits 45 s, but coalesced readers
  may arrive late and need the key still present (the API does NOT
  DEL after read — see Bug #2 plan §3 step 4).

**List bound (`LTRIM -32 -1`).** Under a /kill-all storm or a
coalescing failure, the same deployment could see > 1 RPUSH before
the child drains. The list is bounded at the most-recent 32 entries
so it cannot grow unbounded even if the child is wedged. The
`msai_flatness_pending_list_length` gauge surfaces this — healthy
sustained value is 1; > 1 means coalescing isn't holding.

**Why per-nonce key instead of a shared Redis Stream.** Codex iter-4
P1: a shared `XREADGROUP` consumer group load-balances entries
across consumers, so caller A could XREAD caller B's report and
ACK it as "mismatched," leaving B to time out. Per-nonce keys
eliminate this — each caller GETs only its own key.

**Why the child writes the report (not the supervisor).** Codex
iter-3 P1: the parent `ProcessManager` only owns the `mp.Process`
handle. The `TradingNode` (and its `kernel.cache`) lives inside the
child. There is no in-parent reference to `cache.positions_open()`;
the report must be produced inside the child. The child
opens its own aioredis client at `run_trading_node` startup
(`payload.redis_url`) with `socket_connect_timeout=2.0`/
`socket_timeout=2.0`, and the entire drain is wrapped in
`asyncio.wait_for(5.0)` so a stuck Redis cannot block shutdown.

## Runbook

### Operator observes `broker_flat: false` in `/stop` response

Nautilus's `market_exit()` exhausted `max_attempts` while positions
remained. The deployment is stopped but the broker still holds
shares.

**Action:**

1. Read `remaining_positions` from the response — instrument_id +
   quantity + side per remaining position.
2. Open IB portal (TWS or Web), verify the residual positions
   against the report.
3. Flatten manually via IB UI or `gh cli flatten <instrument>` (if
   you have a flatten-script).
4. After confirming flat: `POST /api/v1/live/resume` to clear the
   risk halt flag (if /kill-all set it).

### Operator observes `broker_flat: unknown` (504 timeout)

The API never received a `stop_report:{nonce}` key within the
45 s (/stop) or 35 s (/kill-all) deadline. Possible causes:

| Cause                                                   | Detection                                                                           | Action                                                                                            |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Child process crashed before draining                   | `live_node_processes.status = failed`                                               | Operator must verify IB portal manually; positions could be flat or not.                          |
| Child hung in `node.stop_async()` (IB socket teardown)  | Heartbeat thread still advancing, but no `flatness_report_written` log line         | Wait 5s for `wait_for` timeout to fire; if no report after that, escalate.                        |
| Redis network partition between child and API           | `redis-cli ping` from API host fails; child's `socket_timeout=2.0` would have fired | Restart Redis, verify connectivity, re-issue `/stop`.                                             |
| Child's redis client failed to open (`redis_url` empty) | Log: `flatness_drain_no_redis_url`                                                  | This is a config bug — `TradingNodePayload.redis_url` must be populated; check `payload_factory`. |

Manual fallback: `redis-cli GET stop_report:{stop_nonce}` from the
backend container — if the key is present (it has a 120 s TTL), the
API timed out but the child did write. If absent, the child never
wrote it.

### Operator observes `msai_flatness_pending_list_length > 1` sustained

Coalescing isn't holding. Most likely an `inflight_stop:{deployment_id}`
TTL race (key expired between SET-NX failure and GET). The
recursion guard in `coalesce_or_publish_stop_with_flatness` covers
the most common case, but sustained > 1 means clients are issuing
many concurrent stops on the same deployment.

**Action:**

- Inspect `msai_flatness_requests_total` vs `msai_flatness_coalesced_total`
  — coalesce-hit rate should be high for sequential stops.
- If a client is mis-retrying (treating 504 as "issue another stop"):
  fix the client. The 504 means "no report yet, but the SIGTERM is
  already in flight — keep polling, don't restart."

### Operator observes child write but API never reads

The 120 s TTL hasn't expired yet. Manually:

```bash
docker exec -it msai-claude-redis redis-cli GET stop_report:{nonce}
```

If the JSON is present and looks correct, the issue is on the API
side — the poll loop returned `None` before the key materialized.
Check `msai_flatness_poll_timeout_total` — if non-zero, see runbook
"broker_flat: unknown" above.

## Metrics (Prometheus / OTEL)

- `msai_flatness_requests_total` — counter, increments per API /stop or /kill-all call.
- `msai_flatness_coalesced_total` — counter, increments when SET-NX returns False (coalesce hit).
- `msai_flatness_poll_timeout_total` — counter, increments when `poll_stop_report` hits its deadline.
- `msai_flatness_report_non_flat_total` — counter, increments when a report has `broker_flat=False`. Each = operator action.
- `msai_flatness_pending_list_length` — gauge labeled by `deployment_id`, set on every RPUSH (post-LTRIM).

**Alert rules** (suggested for prod):

| Alert                      | Condition                                                  | Severity |
| -------------------------- | ---------------------------------------------------------- | -------- |
| FlatnessTimeoutBurst       | `rate(msai_flatness_poll_timeout_total[5m]) > 0.1`         | warn     |
| FlatnessReportNonFlat      | `increase(msai_flatness_report_non_flat_total[1h]) > 0`    | crit     |
| FlatnessPendingListGrowing | `max_over_time(msai_flatness_pending_list_length[5m]) > 1` | warn     |

`msai_flatness_report_non_flat_total > 0` is the high-priority page
— operator must verify the residual broker position via IB portal.

## References

- Implementation plan: `docs/plans/2026-05-13-live-deploy-safety-trio.md` §"Bug #2"
- Council verdict (5-advisor + Codex chairman, 2026-05-13): split PRs via Option D
- Nautilus `Strategy.stop()` / `market_exit()`: `trading/strategy.pyx:404-416, 1773-1799`
- Memory: `feedback_e2e_before_pr_for_live_fixes`
