# Claude — Nautilus Production Hardening (Revision 9 — IMPLEMENTATION-READY)

**Status:** Plan v9 (final sanity pass on Codex v8 findings; review loop CLOSED, implementation begins at Phase 1). Contains the four v8 fixes (pid-fallback, SKIP LOCKED, stale references, failure_kind on remaining writers). **Does not undergo another Codex review round** — remaining marginal risk will be caught during implementation and Phase 5 paper soak.
**Branch:** `feat/claude-nautilus-production-hardening`
**Scope:** `claude-version/` ONLY. The `codex-version/` directory is not touched by this plan; Codex CLI is hardening that codebase independently in parallel.

## References

- `docs/plans/2026-04-06-architecture-review.md` — the architecture review that produced this plan
- `docs/nautilus-reference.md` — deep technical reference on NautilusTrader (60KB, 10 sections, 20 gotchas)
- `docs/nautilus-natives-audit.md` — what Nautilus already provides natively vs what we have to build
- `.claude/rules/nautilus.md` — auto-loaded short-form gotchas list
- `docs/plans/2026-04-06-claude-nautilus-production-hardening.md` (this file)

## What changed in revision 9 (final sanity pass — review loop CLOSED)

Codex v8 review identified 1 P0 + 2 P1 + 1 P2 findings, all in our supervisor/DB glue (no new Nautilus-native issues). v9 is a tightly-scoped final pass that addresses the four remaining findings; **it does not undergo another Codex review round**. Rationale: seven review iterations have converged the architectural direction (Codex explicitly verified every Nautilus-native claim in v6, v7, and v8 as correct). The remaining glue-code issues find one new bug per iteration at diminishing returns. Phase 1 implementation will catch any remaining bugs faster than continued review, and the Phase 5 paper soak is the real validation gate for production readiness.

**Corrections from v8:**

1. **Watchdog consults `self._handles` for pid fallback** (Codex v8 P0). v8's phase-C failure path accepted `pid=NULL` while keeping the live `mp.Process` in `self._handles`. If such a subprocess wedged before self-writing its pid, the watchdog's `if row.pid is not None: os.kill(...)` skipped the SIGKILL entirely but still flipped the row to `failed`. The child survived with a terminal row → a retry could spawn a duplicate. v9 fixes `_watchdog_kill_one` to source the pid from `row.pid OR self._handles[deployment_id].pid`. If neither source yields a pid, the watchdog logs ERROR, pages the operator, and DOES NOT flip the row to `failed` — leaves it for the next iteration to try again. This eliminates the silent-survival window.

2. **Watchdog uses `SELECT FOR UPDATE SKIP LOCKED` + per-row `asyncio.wait_for` safety belt** (Codex v8 P1). v8's `_watchdog_kill_one` had no lock timeout or SKIP LOCKED clause — one row whose lock was held by a concurrent transaction could block the whole serial candidate loop. v9 switches to `with_for_update(of=LiveNodeProcess, skip_locked=True)`: if the row is contended, the SELECT returns nothing and the watchdog skips it for this pass (picks it up 5s later). Wraps each per-row call in `asyncio.wait_for(kill_one, timeout=5)` so a hung Postgres operation can't block the whole loop.

3. **Stale references pruned from task sections and TDD** (Codex v8 P1). Codex found four places where older-revision prose contradicts the v8 summary:
   - The idempotency TDD still asserted `body_mismatch` was cacheable (v7 behavior; v8 changed it to `cacheable=False`)
   - The Phase 4 recovery section widened `HeartbeatMonitor` back to include `starting` (v6 behavior; v7 narrowed it to `ready`/`running`/`stopping`)
   - The 600s stale hard timeout reference (v7 value; v8 raised to 1800s)
   - A `_mark_stale_as_failed` snippet aging out hung builds (v6 behavior)
     v9 updates or deletes each one. If an implementer reads only a task section and misses the pre-phase summary, they no longer get the wrong behavior.

4. **`failure_kind` wired in `_on_child_exit` and `_mark_stale_as_failed`** (Codex v8 P2). v8 added `failure_kind` writes to `_mark_failed` (halt block, spawn_start failure, watchdog kill) and the subprocess finally block, but two paths still wrote `status='failed'` without touching `failure_kind`. v9 finishes the job:
   - `_on_child_exit(deployment_id, exit_code)`: `failure_kind = FailureKind.NONE if exit_code == 0 else FailureKind.SPAWN_FAILED_PERMANENT`
   - `HeartbeatMonitor._mark_stale_as_failed`: writes `failure_kind = FailureKind.UNKNOWN` (post-startup stale — the subprocess died without reporting why; the endpoint only reads failure_kind for pre-ready outcomes anyway, so UNKNOWN is fine here)

**v9 CLOSES the plan review loop.** Further iterations have reached diminishing returns; implementation begins immediately. Any additional edge cases will be caught and fixed as they arise during Phase 1 implementation, and the Phase 5 paper soak is the release gate.

---

## What changed in revision 8

Codex re-reviewed v7 and rejected it with 2 P0 + 2 P1 + 1 P2 findings. **No new Nautilus-native issues** — Codex confirmed "v7 does not add a new Nautilus API dependency; the problems are all in supervisor/DB/Redis glue." The four glue-code bugs are all in the new v7 paths: the watchdog's scan-then-kill-then-update sequence still has a race window, the idempotency `BodyMismatchReservation` branch wrongly treats the reply as cacheable even though the caller doesn't own the reservation, `failure_kind` was added to the schema but the supervisor's `_mark_failed` and the subprocess finally block still don't write it, and cold-read hydration can overwrite fresher pub/sub state. The 600s startup hard ceiling is also too tight for large options universes.

**Architectural corrections from v7:**

1. **Watchdog uses lock-first atomic path** (Codex v7 P0). v7 implemented `scan stale rows → SIGKILL → SELECT FOR UPDATE → UPDATE status='failed'`, which still had a race: between the scan and the SIGKILL, the child could flip to `ready` (subprocess finished startup), `stopping` (concurrent `/stop`), or refresh its heartbeat. After the SIGKILL, the filter `status IN ('starting','building')` would miss the row, the UPDATE would be skipped, and the dead child would leave the row stuck in `ready`/`stopping` — a silent desync. v8 rewrites the path to `SELECT FOR UPDATE inside a single transaction, re-check status AFTER the lock, SIGKILL, UPDATE, COMMIT`. The row-level lock prevents concurrent writers from flipping the status while the kill is in flight. If the re-check shows the row is no longer in a startup status (benign race — healthy completion, heartbeat refresh after the scan, etc.) the watchdog aborts without killing. If it IS still startup, the kill is guaranteed atomic with the UPDATE.

2. **Only the `Reserved` branch owns `commit` / `release`** (Codex v7 P0). v7's `body_mismatch` factory returned `cacheable=True`, and step N of the workflow let the endpoint call `commit()` from any outcome where `cacheable=True`. But `BodyMismatchReservation` means **the caller does not own the reservation** — another request is already in-flight or has already completed. Calling `commit()` there would overwrite the original correct response with a 422, poisoning all subsequent correct retries with the same key. v8 changes two things:
   - `body_mismatch` factory returns `cacheable=False`
   - The endpoint uses pattern matching on the `reserve()` result. **Only the `Reserved(redis_key)` branch** is allowed to touch the store. `CachedOutcome`, `InFlight`, and `BodyMismatchReservation` return their outcome directly without calling `commit` or `release`. The endpoint tracks "I own the reservation" via the `redis_key` from the `Reserved` result, not via `outcome.cacheable`.
3. **`failure_kind` writers wired + safe parser** (Codex v7 P1). v7 added the `failure_kind` column to the schema and had the `/start` endpoint read it to decide cacheability — but I forgot to update the writers. The supervisor's `_mark_failed` and the subprocess's finally block only set `status` / `error_message` / `exit_code`. Result: the endpoint always saw `failure_kind=None` on real failures and couldn't classify anything. v8 wires every failure path:
   - `_mark_failed(row_id, reason, failure_kind: FailureKind)` takes the enum as a required argument
   - ProcessManager.spawn phase B halt-flag block writes `FailureKind.HALT_ACTIVE`
   - ProcessManager.spawn phase B `process.start()` failure writes `FailureKind.SPAWN_FAILED_PERMANENT`
   - ProcessManager.watchdog_loop writes `FailureKind.BUILD_TIMEOUT` (on heartbeat stale OR hard wall-clock backstop)
   - HeartbeatMonitor writes `FailureKind.NONE` (it's a post-startup orphan, not a structured failure — the endpoint reads the live_node_processes row only for pre-ready outcomes)
   - Trading subprocess finally block writes the right kind per exception: `StartupHealthCheckFailed` → `FailureKind.RECONCILIATION_FAILED` (the subprocess doesn't have finer granularity), `BuildTimeoutError` → `FailureKind.BUILD_TIMEOUT`, generic `Exception` → `FailureKind.SPAWN_FAILED_PERMANENT`
   - New `FailureKind.UNKNOWN = "unknown"` variant for defensive parsing
   - `FailureKind.parse_or_unknown(db_string: str | None) -> FailureKind` helper that returns `UNKNOWN` for any value not in the enum (including `None` and stale values from an older worker after a schema migration). The endpoint calls this — never `FailureKind(db_string)` directly.

4. **Cold-read hydration is only-if-still-cold** (Codex v7 P1). v7's `hydrate_from_cold_read` merged into existing positions (so a close event arriving during the cold read could be rolled back) and unconditionally overwrote account state. v8 uses an "only promote if nobody else promoted first" pattern:
   - `hydrate_from_cold_read(deployment_id, *, positions=None, account=None)` checks `is_positions_hydrated` and `is_account_hydrated` AT THE MOMENT OF THE WRITE (not when the caller started the cold read). If the state has been hydrated by another path (StateApplier pub/sub) since the cold read started, the hydrate call is a no-op for that domain.
   - PositionReader's cold-path code: `(1) read from Cache`, `(2) hydrate_from_cold_read`, `(3) return the CURRENT state value (not the cold-read result)`. Even in the rare case where the StateApplier wins the race between the cold read and the hydrate, the caller returns the fresher state — never the stale cold read result.

5. **`startup_hard_timeout_s` default raised to 1800s + per-deployment override** (Codex v7 P2). v7's 600s backstop was tighter than the legitimate slow-build case (large options universes can take 900s+). v8 raises the default to 1800s (30 min) and adds a nullable `startup_hard_timeout_s: Mapped[int | None]` column on `live_deployments` — NULL falls back to the supervisor default. Operators who know they have large options chains can set a per-deployment value.

**v7 → v8 changes (still in effect from prior revisions):**

- All v7 corrections (single startup-liveness authority, heartbeat-based watchdog, per-domain hydration, EndpointOutcome + FailureKind, schema `failure_kind` column)
- All v6 corrections (supervisor watchdog concept, subprocess self-writes pid, MsgSpecSerializer, idempotency TTL, heartbeat-before-build, validated-config hash)
- All v5 corrections (broader identity tuple, INSERT-commit pattern, supervisor halt-flag check, dual pub/sub, canonical trader.is_running, DLQ)
- All prior corrections

## What changed in revision 7

Codex re-reviewed v6 and rejected it with 1 P0 + 3 P1 findings. **Every Nautilus-native claim was explicitly verified** as correct — Codex confirmed `MsgSpecSerializer` construction pattern, `_clients`/`registered_clients` semantics, `kernel.trader.is_running` vs `Kernel.is_running()` (the former is the canonical FSM signal, the latter is `self._is_running` set before the async waits), no harmful phase-C PID race, and the task 1.9 ordering assertion is correctly wired into task 1.8's subprocess-order test. The architectural direction is settled.

The remaining issues are all in _our own_ glue code: the watchdog and heartbeat monitor step on each other, the watchdog deadline is wall-clock instead of no-progress-based, `has_seen` is too coarse in both directions, and the idempotency layer still uses status-code-based branching that has internal contradictions. v7 corrects all four.

**Architectural corrections from v6:**

1. **Single startup-liveness authority.** v6 had the HeartbeatMonitor flip stale `starting`/`building` rows to `failed` after 30s, AND the watchdog SIGKILL them after 180s — and the watchdog query filtered `status IN ('starting','building')`. So a wedged build would: (a) heartbeat monitor flips to `failed` at t+30s, (b) the partial unique index slot is now free, (c) the watchdog query no longer sees the row, (d) `/stop` and `/kill-all` filters don't see it either, (e) the real process is still alive, (f) a retry can spawn a duplicate child. Codex v6 P0. v7 fixes this by making the **watchdog the sole liveness authority during startup**: the HeartbeatMonitor stale-sweep query excludes `starting`/`building` (only scans `ready`/`running`/`stopping`). The watchdog is the only code path that marks a `starting`/`building` row as `failed`, and it does so **only after** SIGKILLing the pid — so the row stays in the active statuses until the process is actually dead.
2. **Watchdog deadline is heartbeat-based, not wall-clock.** v6's watchdog killed rows whose `started_at < now() - 180s` regardless of whether the subprocess was making progress. Codex v6 P1 pointed out that a legitimate slow build (30 options underlyings at 10-30s each; 100 instruments at 10-50s per batch — see `docs/nautilus-reference.md:482,513`) can exceed 180s without being wedged. v7 changes the kill condition to `last_heartbeat_at < now() - stale_seconds`: as long as the subprocess's heartbeat thread is advancing the timestamp, the watchdog considers the subprocess making progress and leaves it alone. A secondary hard wall-clock ceiling at `startup_hard_timeout_s = 600` catches pathological cases where the heartbeat thread is still running but the process is otherwise stuck in a degenerate loop. Default `stale_seconds = 30` so a heartbeat gap > 30s triggers the kill.
3. **Cold-read hydrates `ProjectionState` directly; `has_seen` is removed.** v6's `has_seen` flag had two failure modes (Codex v6 P1): (a) any non-state-changing event (`FillEvent`, `OrderStatusChange`) flipped the flag to True, so `get_open_positions` would return an empty list from the "fast path" even when Redis had the real state; (b) a stream entry filtered before `apply()` never flipped the flag, so the "cold path fires only once" claim was false for those events. v7 drops `has_seen` entirely and instead has `PositionReader`'s cold path **write its results back into `ProjectionState`** via the regular `apply()` dispatcher (pretending the cold-read positions arrived as `PositionSnapshot` events and the cold-read account as an `AccountStateUpdate` event). After the cold read, `ProjectionState` has real data for that deployment, and the next `get_open_positions` call naturally uses the fast path because `state.positions(deployment_id)` returns the populated list. Empty-but-hydrated deployments are represented by an explicit empty dict in the positions map, which the fast path serves without touching Redis.
4. **`EndpointOutcome` dataclass replaces status-code-based idempotency branching.** v6's `commit_terminal` allowlisted `{201, 422}` but the workflow docstring told callers to call `commit_terminal(503, ...)` on permanent failure — the helper would throw (Codex v6 P1). The workflow also distinguished "permanent 503" from "transient 503" by parsing the detail string (fragile), and the already-active branch returned 200 but the docstring said `commit_terminal(201, ...)` (status code mismatch). v7 introduces a structured outcome:

   ```python
   class FailureKind(StrEnum):
       NONE = "none"
       HALT_ACTIVE = "halt_active"
       SPAWN_FAILED_PERMANENT = "spawn_failed_permanent"
       RECONCILIATION_FAILED = "reconciliation_failed"
       BUILD_TIMEOUT = "build_timeout"
       API_POLL_TIMEOUT = "api_poll_timeout"
       IN_FLIGHT = "in_flight"

   @dataclass(slots=True, frozen=True)
   class EndpointOutcome:
       status_code: int
       response: dict
       cacheable: bool                    # True → commit_terminal, False → release
       failure_kind: FailureKind = FailureKind.NONE
   ```

   The endpoint's branches produce `EndpointOutcome` instances. The idempotency layer's `commit_terminal()` is renamed to `commit()` and simply checks `outcome.cacheable` — no status-code allowlist, no string parsing. Transient outcomes (`HALT_ACTIVE`, `API_POLL_TIMEOUT`, `IN_FLIGHT`) set `cacheable=False` and trigger `release()`. Permanent outcomes (`SPAWN_FAILED_PERMANENT`, `RECONCILIATION_FAILED`, `BUILD_TIMEOUT`, happy path) set `cacheable=True` and trigger `commit()`. The already-active branch produces `EndpointOutcome(status_code=200, cacheable=True, ...)` — cached correctly as 200, not 201.

**v6 → v7 changes (still in effect from prior revisions):**

- All v6 corrections (supervisor build watchdog, subprocess self-writes pid, correct `MsgSpecSerializer` signature, idempotency TTL, heartbeat-before-build, validated-config hash)
- All v5 corrections (broader identity tuple, INSERT-commit pattern, supervisor halt-flag check inside spawn, dual pub/sub fan-out, canonical `trader.is_running` signal, DLQ)
- All v4 corrections
- All v3 corrections
- All v2 corrections
- All v1 corrections

## What changed in revision 6

Codex re-reviewed v5 and rejected it with 2 P0 + 2 P1 + 2 P2 + 1 P3 findings — noticeably fewer than v4's rejection (3 P0 + 3 P1 + 2 P2). The v5 architectural direction held up: Codex explicitly confirmed `kernel.trader.is_running` as the canonical signal (`system/kernel.py:1014-1037`), `Component.is_running` semantics (`common/component.pyx:1768-1779`), `data_engine.check_connected()` / `exec_engine.check_connected()` as real methods (`data/engine.pyx:296`, `execution/engine.pyx:269`), per-client `reconciliation_active` (`live/execution_client.py:136`), and the four-argument `CacheDatabaseAdapter(trader_id, instance_id, serializer, config)` signature (`cache/database.pyx:132-166`).

v5's remaining issues were tactical: a thread-level build timeout that can't actually stop synchronous C code; a phase-C failure path that breaks `/stop` after a supervisor restart; a serializer class/signature mismatch I didn't verify directly; an idempotency TTL shorter than the startup path; an internal contradiction about heartbeat ordering between decision #17 and task 1.9; and the PositionReader cold path misfiring for empty-but-known state. v6 corrects all seven.

**Author note: further Nautilus 1.223.0 source verification for v6.** Before writing v6 I directly read:

- `nautilus_trader/serialization/serializer.pyx:36-62` — `MsgSpecSerializer.__init__(encoding, timestamps_as_str=False, timestamps_as_iso8601=False)`. `encoding` is a **module** (e.g. `msgspec.msgpack`), not a string. The class uses `encoding.encode` and `encoding.decode` internally.
- `nautilus_trader/system/kernel.py:309-319` — Nautilus itself constructs the serializer as `MsgSpecSerializer(encoding=msgspec.msgpack if encoding == "msgpack" else msgspec.json, timestamps_as_str=True, timestamps_as_iso8601=config.cache.timestamps_as_iso8601)`. v6 matches this exactly.
- `nautilus_trader/execution/engine.pyx:147, 204-214` — `self._clients: dict[ClientId, ExecutionClient]` (private), `registered_clients` property returns `list[ClientId]` (IDs only). The public methods `check_connected()` (line 269-283) iterate `_clients.values()` internally. For the diagnose helper (v6 task 1.8), we access the private `_clients` dict directly since we're in the SAME process that built it — this is acceptable because the diagnose helper is strictly internal and runs only inside the subprocess that owns the kernel.
- `nautilus_trader/portfolio/portfolio.pyx:218` — `self.initialized = False`. Public attribute, flipped to `True` after `_await_portfolio_initialization` succeeds.
- `nautilus_trader/execution/engine.pyx:269-283` — `check_connected()` is a `cpdef bint` method that iterates `_clients.values()` and returns True only if every client's `is_connected` is True. Each `ExecutionClient` has an `is_connected` property (from `Component`).

**Architectural corrections from v5:**

1. **Build watchdog is supervisor-side (process-level), not subprocess-side (thread-level).** v5 wrapped `node.build()` in `asyncio.wait_for(loop.run_in_executor(None, node.build), timeout=120)`. Codex v5 P0 flagged the obvious: `asyncio.wait_for` cancels the awaiter, not the executor thread; a wedged C-side IB build keeps running after the row is marked failed, and the next retry can spawn a duplicate child while the old thread is still wedged. v6 removes the `asyncio.wait_for` wrapper (the subprocess just calls `node.build()` normally) and moves the watchdog to the **supervisor**. `ProcessManager.watchdog_loop()` tracks a per-child deadline (default `build_timeout_s + startup_health_timeout_s = 180s`); if a child hasn't reached `status='ready'` or `status='failed'` by the deadline, the watchdog SIGKILLs it. Process-level supervision can always stop a wedged child, thread-level cannot.
2. **The subprocess self-writes its pid as its first DB action.** v5's phase-C failure path accepted `pid=NULL` and relied on the supervisor's in-memory handle map for `stop()`. Codex v5 P0 found that a supervisor restart wipes the map, after which `stop()` reads `row.pid=None` and returns success without signaling, leaving an unstoppable subprocess. v6 has the subprocess write `os.getpid()` to `live_node_processes.pid` immediately after connecting to Postgres, BEFORE anything else. The supervisor also still does a fallback UPDATE in phase C for belt-and-suspenders (some subprocess errors may prevent the child from even reaching the self-write). After the self-write, the row has a real pid on every code path, so `stop()` / `kill-all` via pid always work even across supervisor restarts.
3. **`MsgSpecSerializer` with the correct signature.** v5 mixed up class names (`MsgPackSerializer` vs `MsgSpecSerializer`) and passed a string for the `encoding` parameter. The real class is `MsgSpecSerializer` in `nautilus_trader/serialization/serializer.pyx:36`, and `encoding` is a **module** (e.g. `msgspec.msgpack`). v6 uses exactly the same construction Nautilus uses internally (kernel.py:313-317):
   ```python
   import msgspec
   from nautilus_trader.serialization.serializer import MsgSpecSerializer
   serializer = MsgSpecSerializer(
       encoding=msgspec.msgpack,
       timestamps_as_str=True,
       timestamps_as_iso8601=False,
   )
   ```
4. **`exec_engine._clients.values()` for per-client diagnosis** (with a clear "private access, acceptable in-process" comment). v5's diagnose helper used `kernel.exec_engine.registered_clients.values()` which doesn't work because `registered_clients` is a `list[ClientId]`, not a dict. The real dict of ExecutionClient objects lives on the private `_clients` attribute. Since the diagnose helper runs **inside the same process** that constructed the kernel, private-attribute access is acceptable (documented in the code comment).
5. **Idempotency reservation TTL extended to cover the full startup path; transient responses NOT cached.** v5's 60-second reservation TTL was shorter than `build_timeout_s + startup_health_timeout_s + api_poll_timeout_s` (potentially 180+ seconds). v5 also cached 504 Gateway Timeout responses for 24 hours, which contradicted the "caller can retry with the same key" guidance. v6 sets the reservation TTL to 300 seconds (covers the worst-case startup with margin). Only **terminal** responses are cached (`201 Created` with a ready deployment, `503 Service Unavailable` with a permanently-failed deployment). Transient responses (`425 Too Early`, `504 Gateway Timeout`, `503 "kill switch active"`) **release** the reservation instead of caching — so retries with the same key can actually re-attempt.
6. **`ProjectionState` tracks "seen" deployments separately from "has positions".** v5's `PositionReader.get_open_positions` used `if positions:` as the fast-path condition — for an idle deployment with zero open positions, the fast path always missed and every request fell through to `cache.cache_all()` (Codex v5 P2). v6 adds `ProjectionState.has_seen(deployment_id) -> bool`: flipped to True on the first `apply()` call for that deployment, regardless of whether the event changed position state. `PositionReader.get_open_positions` fast-path condition becomes `if self._state.has_seen(deployment_id)`, so a confirmed-empty deployment serves the empty list from in-memory. Only truly-cold workers (haven't seen any event yet) hit the Cache rebuild path — and only ONCE per deployment per worker restart.
7. **Heartbeat ordering contradiction resolved.** v5 said "heartbeat starts BEFORE `node.build()`" in decision #17 and the task 1.8 docstring, but task 1.9 still said it starts AFTER `node.build()`. v6 fixes task 1.9 to cross-reference task 1.8 for the correct ordering and removes the stale claim.
8. **`config_hash` is computed from the Pydantic-validated config model**, not the raw request dict. Semantically-identical configs (`{"x": "5"}` vs `{"x": 5}`) produce the same hash. The helper `compute_config_hash(config_model)` takes a Pydantic BaseModel, calls `.model_dump(mode="json")`, and hashes the canonical JSON.

**v5 → v6 changes (still in effect from prior revisions):**

- All v5 corrections (broader identity tuple, INSERT-commit → halt-check → spawn → UPDATE-commit pattern, supervisor-side halt-flag check inside spawn, dual pub/sub state fan-out, `kernel.trader.is_running` as the canonical signal, heartbeat-before-build in concept, DLQ + delivery-count cap)
- All v4 corrections
- All v3 corrections
- All v2 corrections
- All v1 corrections

## What changed in revision 5

Codex re-reviewed v4 and rejected it with 3 P0 + 3 P1 + 2 P2 findings. The v4 architectural direction held up — Codex confirmed `CacheConfig` import path, `Portfolio.total_pnls()`/`net_exposures()` plurals, and `manage_stop = True` are all correct. The remaining problems were in: identity tuple coarseness, the `ProcessManager.spawn` transaction pattern, supervisor-side kill-switch enforcement, multi-uvicorn-worker projection state correctness, the post-start health check (which was overengineered against attributes that don't exist), the `CacheDatabaseAdapter` constructor signature, in-flight idempotency, and PEL poison-message handling. v5 corrects all eight.

**Author note: Nautilus 1.223.0 source verification.** Before writing v5, I read `nautilus_trader/system/kernel.py:1001-1037`, `nautilus_trader/live/node.py:174-282`, `nautilus_trader/common/component.pyx:1768-1779`, `nautilus_trader/cache/database.pyx:101-167`, and `nautilus_trader/live/execution_client.py:136`. Findings:

- `kernel.start_async()` DOES wait internally (engines connected → reconciliation → portfolio init → `_trader.start()`), but each await silently early-returns on failure. So v3/v4's instinct that "start_async returning is not proof" was correct, but v4 polled the wrong attributes.
- The canonical "fully started" signal is `kernel.trader.is_running`. The trader's FSM transitions to RUNNING only inside the LAST line of `start_async` (`self._trader.start()`), which is reached only on full success. If any await failed silently, the trader FSM stays in READY. This is the simple, correct check.
- `kernel.is_running()` is the WRONG signal — it returns `self._is_running`, which is set to True at the very TOP of `start_async`, before any await. Useless for readiness.
- Engine connectivity uses methods: `data_engine.check_connected()` and `exec_engine.check_connected()` (NOT `is_connected` attributes).
- `reconciliation_active` is a per-`LiveExecutionClient` flag (`live/execution_client.py:136`), not engine-level.
- `CacheDatabaseAdapter.__init__` requires `(trader_id, instance_id, serializer, config)` — four positional args, all required.
- `node.build()` runs `_builder.build_data_clients/build_exec_clients` BEFORE `start_async`. IB client construction can hang on contract-detail fetches. Heartbeats need to start BEFORE `node.build()` and the HeartbeatMonitor must include `'building'` in its stale-sweep query (v4 excluded it).

**Architectural corrections from v4:**

1. **Broader identity tuple for `deployment_slug` derivation.** v4's `(started_by, strategy_id, paper_trading, instruments_signature)` was too coarse — a config change, code-hash change, account change, or strategy-class rename reused the same `trader_id` and `StrategyId`, so Nautilus would reload incompatible old state into a materially different deployment. It also made two parameterizations of the same strategy on the same instruments impossible. v5's identity tuple is `(started_by, strategy_id, strategy_code_hash, config_hash, account_id, paper_trading, instruments_signature)`, hashed via canonical-JSON sha256 to produce `identity_signature`. The unique index becomes `UNIQUE(identity_signature)`. Any change in any field → new identity_signature → new deployment_slug → cold start with no state reload. Same identity → existing deployment_slug → warm restart with state reload. **Operator UX:** "I tweaked a parameter" produces a new deployment row + slug + cold start (intentionally — stale state should not contaminate a tweaked strategy); "I restarted the same setup" produces a warm restart.
2. **`ProcessManager.spawn` uses INSERT-commit → halt-check → spawn → UPDATE-commit, NOT a single transaction.** v4 wrapped the entire spawn (DB INSERT + `process.start()` + DB UPDATE) in one `session.begin()`. If `process.start()` succeeded but the post-spawn UPDATE/COMMIT failed, the transaction rolled back, leaving a live trading subprocess with no committed row — and the next retry would launch a duplicate. v5 splits this into three transactions: (a) INSERT row with `pid=NULL`, COMMIT (claims the partial unique index slot); (b) check halt flag, then `process.start()` outside any transaction; (c) UPDATE pid in a fresh transaction. If anything fails after INSERT but before UPDATE, the row stays in `status='starting'` with `pid=NULL`, the heartbeat monitor times it out and flips it to `failed`, and the next retry succeeds. v5 also adds `'stopping'` to the active-states query (v4 documented it but the actual SELECT omitted it).
3. **Supervisor-side halt-flag check inside `ProcessManager.spawn`.** v4's `/kill-all` set the halt flag and pushed stop commands, but the halt flag was only re-checked at the HTTP `/start` entry — any start command already queued in `msai:live:commands` (or later reclaimed from the PEL) would still launch. v5 has `ProcessManager.spawn` call `await redis.exists("msai:risk:halt")` as the LAST step before `process.start()`. If the halt flag is set, the row is updated to `status='failed'`, `error_message='blocked by halt flag'`, and the spawn is a no-op (the command is still ACKed because it was successfully handled — there's nothing to retry until the operator clears the halt).
4. **Multi-worker `ProjectionState` via state-update pub/sub.** v4 had each uvicorn worker join the SHARED `msai-projection` consumer group, so a given stream entry was consumed by exactly ONE worker — and only that worker's in-memory `ProjectionState` got updated. The other workers serving snapshot reads saw stale state. v5 adds a second pub/sub channel `msai:live:state:{deployment_id}` (separate from the WebSocket-fanout `msai:live:events:{deployment_id}` channel for clarity). The projection consumer publishes the translated event to BOTH channels. Every uvicorn worker runs a `state_applier` background task that subscribes to the state channel and calls `ProjectionState.apply(event)` on its own local instance. The consumer-group ensures the stream is consumed exactly once; the pub/sub ensures every worker's in-memory state is updated.
5. **Post-start health check is just `kernel.trader.is_running`.** v4's check polled `data_engine.is_connected`, `exec_engine.is_connected`, and engine-level `reconciliation_active` — none of which exist in Nautilus 1.223.0 (they're per-client method calls, not attributes). v5 simplifies: `wait_until_ready` polls `kernel.trader.is_running` (the canonical FSM-RUNNING signal that only trips after `_trader.start()` succeeds at the end of `start_async`). The diagnose helper for failure messages uses the REAL Nautilus accessors (`data_engine.check_connected()` method, `exec_engine.check_connected()` method, per-client `reconciliation_active` from `LiveExecutionClient`, `portfolio.initialized`).
6. **Heartbeat thread starts BEFORE `node.build()`** (was after in v4). `node.build()` constructs IB clients and can issue contract-detail fetches that hang on network failures. v4 started the heartbeat AFTER `build()` and excluded `'building'` from the HeartbeatMonitor's stale sweep, so a hung build wedged the deployment forever. v5 starts the heartbeat in the `'building'` state and includes `'building'` in the stale sweep — a hung build now ages out via the heartbeat monitor and the supervisor's reap loop sees the dead child.
7. **`CacheDatabaseAdapter` constructor signature corrected.** v4 showed `CacheDatabaseAdapter(trader_id=..., config=...)`, which omits the required `instance_id` and `serializer` arguments. v5 uses the verified signature `CacheDatabaseAdapter(trader_id, instance_id, serializer, config)` with `instance_id = UUID4(uuid.uuid4().hex)` per request and `serializer = MsgSpecSerializer(encoding="msgpack", timestamps_as_str=True)` (matches the live trading subprocess's serializer config so the read encoding matches the write encoding).
8. **Idempotency-Key uses atomic SETNX in-flight reservation.** v4's idempotency cache was post-hoc only — two concurrent retries with the same `Idempotency-Key` could both miss the cache and both publish before either response was cached. v5 uses `SET msai:idem:start:{user_id}:{key_hash} <reservation_marker> NX EX 60` as the FIRST step in the endpoint. Concurrent retries get a 425 Too Early until the first one completes and writes the real cached response. The key is also user-scoped (`{user_id}:{key_hash}` not just `{key_hash}`) — minor for single-user but eliminates a future cross-principal leak risk if the system goes multi-user.
9. **PEL DLQ + delivery-count cap.** v4's `LiveCommandBus` and projection consumer reclaimed stale entries forever. A permanently malformed message would bounce in the PEL infinitely. v5 adds: (a) when claiming an entry via `XAUTOCLAIM`, the `delivery_count` returned is checked against `max_delivery_attempts` (default 5); (b) entries that exceed are `XADD`ed to the dead-letter stream `msai:live:commands:dlq` (or `msai:live:events:dlq:{deployment_id}` for projection events) with the original entry preserved and a `dlq_reason` field; (c) the entry is then `XACK`ed on the original stream; (d) operator alerts on every DLQ entry. Tests prove a poison message lands in the DLQ after exactly N attempts.

**v4 → v5 changes (still in effect from prior revisions):**

- All v4 corrections (stable deployment_slug pattern, PEL recovery via XAUTOCLAIM, post-start gate concept, schema migration via 1.1b, ProjectionState concept, push-based kill switch, restart test via testcontainers Redis, supervisor handle map for instant exit detection)
- All v3 corrections (dedicated live-supervisor container, stream_per_topic=False, Redis pub/sub for fan-out, manage_stop=True, parity harness redesign)
- All v2 corrections (Nautilus natives audit, RiskAwareStrategy mixin not subclass, no PositionSnapshotCache, simpler crash recovery)
- All v1 corrections

## What changed in revision 4

Codex re-reviewed v3 and rejected it with 3 new P0 + 5 P1 + 2 P2 findings. Container topology and ownership are now directionally correct, but v3 had errors in Redis Streams semantics, command idempotency, the readiness gate, and the identity/schema model. v4 corrects these.

**Architectural corrections from v3:**

1. **Stable `deployment_slug` decoupled from `live_deployments.id`.** v3 derived `trader_id`/`order_id_tag` from `deployment_id`, but the live_deployments model creates a fresh row on every restart, so the deterministic identity changed across restarts and Phase 4 state reload broke. v4 adds a stable `deployment_slug` (16 hex chars = 64 bits, ~no collisions until 4 billion deployments) on the `live_deployments` row that is computed once at first deployment and reused across restarts. The stable slug is also persisted on the strategy row for cross-restart logical identity. Per-restart per-process state lives only in `live_node_processes` (run records), not in `live_deployments`.
2. **Redis Streams PEL recovery via `XAUTOCLAIM`.** v3 assumed un-ACKed messages are auto-redelivered to a new consumer in the same group. They are not — they sit parked in the Pending Entries List (PEL) until a consumer explicitly claims them. v4 uses `XAUTOCLAIM` (Redis ≥ 6.2) on consumer startup to reclaim entries idle longer than `min_idle_time_ms`. Both `LiveCommandBus.consume` (1.6) and the projection consumer (3.4) implement explicit PEL recovery. Tests verify that an un-ACKed message IS picked up by a new consumer after the recovery sweep, NOT by automatic redelivery.
3. **Idempotency at every layer.** v3's `ProcessManager.spawn` had no "already active" guard and `run_forever()` ACKed in `finally` even on failure. v4 adds: (a) a database-level unique partial index on `live_node_processes(deployment_id) WHERE status IN ('starting','building','ready','running','stopping')` so duplicate spawns fail at the DB; (b) the supervisor's command handler ACKs **only on success**, leaving failed messages in the PEL for `XAUTOCLAIM` recovery; (c) the `/api/v1/live/start` endpoint accepts an `Idempotency-Key` HTTP header and short-circuits to return the existing deployment if a duplicate retry arrives within the idempotency TTL.
4. **Post-start health check before `status="ready"`.** v3 treated `kernel.start_async()` returning as proof that reconciliation completed and trading was ready. v4 adds an explicit health check: after `kernel.start_async()` returns, the subprocess polls `node.is_running and trader.is_running and data_engine.is_connected and exec_engine.is_connected and not exec_engine.reconciliation_active` for up to `startup_health_timeout_seconds` (default 60). Only if **all** conditions are true does it write `status="ready"`. On timeout, the subprocess writes `status="failed"`, error_message includes which condition failed, and exits.
5. **`live_node_processes.pid` is nullable; `building` added to status enum.** v3's schema was inconsistent with its workflow (row is inserted before `process.start()` but pid was non-nullable). v4 fixes the column nullability and adds `building` to the status enum which the subprocess writes during the `kernel.build()` phase.
6. **`live_deployments` schema additions in a dedicated migration.** v3 referenced new columns (`trader_id`, `account_id`, `message_bus_stream`, `strategy_id_full`, `deployment_slug`) without a task that creates them. v4 adds task 1.1b which migrates the existing `live_deployments` model to add these columns and makes the existing model docstring "A new deployment row is created each time a strategy is (re-)started" obsolete — the new model reuses rows across restarts when keyed by stable `deployment_slug`.
7. **PositionReader rebuild model corrected.** v3's `Cache(database=adapter)` then `cache_all()` once is wrong: the `Cache` does not subscribe to Redis updates and would drift after the first read. v4's PositionReader builds a fresh `CacheDatabaseAdapter` and `Cache` per request, calls `cache_all()`, reads, and disposes — so every read is fresh. The import path is corrected to `from nautilus_trader.cache.config import CacheConfig` (not `nautilus_trader.common.config`). For the WebSocket initial snapshot the cost is acceptable (one snapshot per connect); for high-frequency reads, the projection consumer maintains an in-memory `dict[deployment_id, PositionState]` populated from the message bus stream, which the WebSocket already uses for live updates anyway.
8. **`RiskAwareStrategy` uses correct portfolio API names.** `portfolio.total_pnl()` and `portfolio.net_exposure()` take `InstrumentId` (per-instrument), not `Venue`. The venue-level aggregate methods are `portfolio.total_pnls(venue)` and `portfolio.net_exposures(venue)` — plural — and return `dict[Currency, Money]`. v4 fixes the mixin to use the plural forms for venue-level checks and the singular forms for per-instrument checks.
9. **Restart-continuity test runs against testcontainers Redis.** v3 invented an "on-disk KV-store StateSerializer" that doesn't exist in this Nautilus install — `DatabaseConfig` here is Redis-only. v4's restart test brings up a testcontainers Redis, points two consecutive `BacktestNode` runs at it via `CacheConfig.database = redis`, and verifies state persists across runs. Phase 4 Scenario D (live-feed restart assertion) is dropped — it's non-deterministic and the BacktestNode-twice test already proves the save/load contract.
10. **Supervisor keeps in-memory `dict[deployment_id, mp.Process]`.** v3 threw away child handles and relied solely on heartbeat. v4 keeps the handle map (parent and child are in the same namespace, so `Process.is_alive()`/`exitcode` ARE meaningful and give instant exit detection). Heartbeat is used only as the recovery/discovery signal after a supervisor restart — when the live handles are gone but the rows survive.
11. **Kill switch is push-based, not bar-poll.** v3's "strategy reads halt flag on `on_bar`" allows up to one bar of lag (up to a minute). v4 makes `/kill-all` publish a `stop` command for every running deployment to the supervisor (immediate SIGTERM → `manage_stop=True` flatten), AND set the persistent halt flag (so a future `/start` is rejected until `/resume`), AND have the audit hook in the strategy mixin reject any new orders the moment the in-process halt flag is observed. Three layers, no bar-poll lag.

**v3 → v4 changes (still in effect from prior revisions):**

- Dedicated `live-supervisor` Docker container (not arq-hosted)
- `stream_per_topic = False` — one deterministic Redis stream per trader
- Redis pub/sub for WebSocket fan-out (multi-uvicorn-worker correctness)
- FastAPI imports `nautilus_trader` for the Cache Python API
- `StrategyConfig.manage_stop = True` for native flatten on stop
- Parity harness: determinism + config round-trip + intent contract
- Heartbeat-only liveness for **cross-container recovery** (not for in-process exit detection — see #10 above)
- Custom `RiskEngine` subclass deleted; replaced with `RiskAwareStrategy` mixin
- `PositionSnapshotCache` deleted (use Nautilus Cache + projection state)
- `buffer_interval_ms = None` (write-through)
- Audit `client_order_id` correlation key
- Strategy code hash from file bytes
- `instrument_cache.trading_hours` JSONB column
- Phase 1 tasks 1.7-1.11 sequential

## What changed in revision 3

Codex re-reviewed v2 and rejected it with 2 new P0 + 7 P1 findings — all in the area of container topology and process ownership. v3 corrects the architectural mistakes and uses Nautilus features v2 was still reinventing.

**Architectural corrections from v2:**

1. **Dedicated `live-supervisor` Docker service (Option A).** v2 tried to host the supervisor as an arq startup task, but arq awaits `on_startup` completion BEFORE entering its poll loop — a "loops forever" startup would block the worker. v3 adds a third backend container alongside `backend` and `backtest-worker`: `live-supervisor`. It runs `python -m msai.live_supervisor` as its own entrypoint and consumes the Redis command stream directly. Trading subprocesses are children of this container. When the supervisor restarts, its children die — we accept this as a full node restart with broker reconciliation (which is fast and automatic).
2. **Heartbeat is the authority for liveness, not PID probing.** v2 proposed `os.kill(pid, 0)` from FastAPI to detect orphaned subprocesses, but FastAPI is in a different container namespace from the trading subprocess — PIDs are meaningless across containers. v3 uses heartbeat freshness (`last_heartbeat_at < now - 30s` → orphaned) as the sole liveness check.
3. **Deterministic `trader_id` / `strategy_id` / `order_id_tag`** derived from `deployment_id`. v2 never set these; Nautilus defaults to `TRADER-001` (collisions) and strategy IDs become unstable. v3 locks them in: `trader_id = f"MSAI-{deployment_id.hex[:8]}"`, `order_id_tag = deployment_id.hex[:8]`. This is also why Phase 4 state reload now works — state is keyed by the deterministic `strategy_id`, not `deployment_id`.
4. **`stream_per_topic = False`** so Nautilus publishes to ONE stream per trader (`trader-MSAI-{id}-stream`) rather than N streams per (topic, strategy). `stream_per_topic=True` combined with strategy-scoped topics means FastAPI can't subscribe before the stream exists (wildcard `XREADGROUP` is not a thing). v3 uses one stream per trader, deterministic name, FastAPI registers on deployment start.
5. **Redis pub/sub for WebSocket fan-out**, not in-memory queues. The backend runs with `--workers 2`, so in-memory queues mean a WebSocket client only sees events from the uvicorn worker that consumed them. v3 uses a Redis pub/sub channel per deployment.
6. **Nautilus Cache Python API** instead of raw Redis key reads. v2 suggested reading Nautilus's Redis keys directly from FastAPI, but those names are internal implementation details. v3 imports `nautilus_trader` in FastAPI and uses a transient `Cache` backed by the same `CacheDatabaseAdapter`.
7. **`manage_stop = True` native flatten**, not custom `on_stop`. Nautilus has a built-in market-exit loop triggered by `StrategyConfig.manage_stop = True`. v2's custom `on_stop` was reinventing this.
8. **Parity harness redesigned.** v2 planned to "feed bars into a TradingNode against IB paper" — this doesn't exist in Nautilus. v3 replaces it with three simpler tests:
   - **Determinism test**: same strategy, same bars, run BacktestNode twice, assert identical trade lists
   - **Config round-trip test**: load strategy via `ImportableStrategyConfig` with the live config schema, assert instantiation succeeds
   - **Intent capture contract test**: backtest emits `(timestamp, instrument, side, qty)` tuples; paper soak (Phase 5) is what catches live divergence, not this harness
9. **Restart test via BacktestNode twice.** v2 planned to restart a live TradingNode subprocess, which requires a deterministic bar feeder we don't have. v3 uses BacktestNode for both legs: run 1 saves state after N bars, run 2 loads state and processes bar N+1, asserts no duplicate order.

**v1 → v2 changes (still in effect):**

- Custom `RiskEngine` subclass DELETED (kernel can't use it); replaced with strategy-side mixin
- `PositionSnapshotCache` DELETED (Nautilus Cache already does this)
- Cache rehydration smoke test DELETED (automatic)
- Crash recovery simplified to orphaned-process detection only
- Reconciliation gating replaced with `status="ready"` marker after `kernel.start_async()` returns
- `buffer_interval_ms = 0` → `None`
- Redis stream topic names corrected
- Consumer groups with persisted offsets
- Audit `client_order_id` correlation key
- Strategy code hash from file bytes (not git)
- Phase 1 E2E uses deterministic smoke strategy
- `instrument_cache.trading_hours` JSONB column
- `GET /api/v1/live/status/{deployment_id}` route added
- Phase 1 tasks 1.7-1.11 sequential (not parallel)

## Goal

Production-harden the Claude implementation of MSAI v2 so it can safely run a personal hedge fund:

- Real Nautilus `TradingNode` for live trading via Interactive Brokers (currently a stub)
- Real security master that handles stocks, futures, options, indexes, FX (currently fake `TestInstrumentProvider.equity(SIM)`)
- Backtest and live use the **same** strategy code, the **same** instrument IDs, the **same** event contract
- Real-time positions, fills, and PnL visible in the dashboard, streamed from Nautilus's own message bus
- Risk runs in the order path with real inputs (currently hardcoded zeros)
- Crash recovery and broker reconciliation on restart (mostly automatic via Nautilus, we wire only the orphan detection)
- Order audit trail for every submission attempt with `client_order_id` correlation
- 30-day paper soak as a release gate before any real money

## Non-Goals

- The `codex-version/` codebase. This plan does not modify it.
- Multi-user / multi-tenant support.
- Distributed deployment beyond a single Azure VM (deferred to Phase 6+).
- Crypto venues. IB-supported asset classes only.

## Approach

Five phases. Each phase ends with a demonstrable improvement and a docker-based E2E verification. Phases are strictly sequential. Tasks within a phase parallelize only if explicitly noted (revision 2 corrected several false-parallelization claims from revision 1).

Every task uses TDD: failing test first, then implementation, then refactor.

**Iron rule:** If Nautilus already does it, we do not build it. We only configure it. The natives audit (`docs/nautilus-natives-audit.md`) is the authoritative reference for "already provided vs we have to build" decisions.

---

## Pre-Phase Decisions (Locked Before Phase 1)

These choices are locked here so every phase can rely on them.

**1. Canonical symbology: `IB_SIMPLIFIED`**
Live IB instruments use the form `<symbol>.<exchange>` — `AAPL.NASDAQ`, `EUR/USD.IDEALPRO`, `ESM5.XCME`. Set `InteractiveBrokersInstrumentProviderConfig.symbology_method = SymbologyMethod.IB_SIMPLIFIED`.

**2. Backtest instruments use the same canonical IDs as live.**
A backtest of AAPL uses `AAPL.NASDAQ`. The current `*.SIM` rebinding in `claude-version/backend/src/msai/services/nautilus/instruments.py` is removed in Phase 2.

**3. Live IB venue suffixes are real exchanges.**
Equities → `NASDAQ`, `NYSE`, `ARCA`. FX → `IDEALPRO`. Futures → `XCME`, `XCBT`, `GLOBEX`. Options → underlying exchange. Indexes → `CBOE`, `XNAS`.

**4. Nautilus IB client factory key stays `"IB"`.**
This is the registration key for `node.add_data_client_factory("IB", ...)` and `node.add_exec_client_factory("IB", ...)`. Not the venue.

**5. Audit + structured logging start in Phase 1.**
We need them while debugging the live path.

**6. Trading subprocesses are hosted by a dedicated `live-supervisor` Docker service.**

Neither FastAPI nor the arq worker owns the trading subprocess. A third backend container runs a long-running supervisor whose only job is to consume the Redis command stream and spawn `TradingNode` subprocesses.

The control plane (Option A):

```
┌───────────────┐           ┌────────────────┐      ┌─────────────────────┐
│  FastAPI      │           │  backtest      │      │  live-supervisor    │
│  backend      │           │  worker (arq)  │      │  (standalone)       │
│               │           │                │      │                     │
│ POST /start ──┼──┐        │  handles       │      │ consumes Redis      │
│ POST /stop    │  │        │  backtest +    │      │ command stream      │
│ GET /status   │  │        │  ingest jobs   │      │ via consumer group  │
│    ▲          │  │        │                │      │          │          │
│    │          │  │        │                │      │          │ spawn    │
│    │ read     │  │        │                │      │          v          │
└────┼──────────┘  │        └────────────────┘      │   ┌─────────────┐   │
     │             │                                │   │ TradingNode │   │
     │         Redis stream msai:live:commands      │   │ subprocess  │   │
     │         ┌──────────────────────────┐         │   │ (mp.Process │   │
     │         │ {"action":"start",...}   │         │   │  spawn)     │   │
     │         │ {"action":"stop",...}    │         │   │             │   │
     │         └──────────────────────────┘         │   └──────┬──────┘   │
     │                                              │          │ heartbeat│
     │         Postgres live_node_processes         │          │          │
     └───────── pid, status, last_heartbeat_at  ◄───┼──────────┘          │
                                                    └─────────────────────┘
```

Service-level behavior:

- **FastAPI backend** publishes `{"action": "start", "deployment_id": ..., ...}` commands to the `msai:live:commands` Redis stream via `XADD`. It **never** spawns subprocesses. `GET /status/{deployment_id}` reads from the `live_node_processes` table.
- **backtest-worker (arq)** is unchanged from today — it only handles backtest and ingest jobs. It does NOT host the live supervisor (Codex v2 P0: arq awaits `on_startup` before its poll loop, so a forever-loop startup task would deadlock the worker).
- **live-supervisor** is a new Docker service. Its entrypoint is `python -m msai.live_supervisor` and it runs `live_supervisor.main.run_forever()`. The supervisor:
  1. Joins the `msai-live-supervisor` consumer group on `msai:live:commands`
  2. Consumes commands via `XREADGROUP`, blocking with a 5-second timeout
  3. On `start`: writes a `live_node_processes` row with `status="starting"`, then calls `multiprocessing.get_context("spawn").Process(target=_trading_node_subprocess, args=(payload,)).start()`, updates the row with the spawned pid, ACKs the stream message
  4. On `stop`: reads the pid from `live_node_processes`, sends `SIGTERM`, waits for `status="stopped"` or timeout (then `SIGKILL`), ACKs the stream message
  5. Periodically scans `live_node_processes` for rows whose `last_heartbeat_at` is older than 30 seconds and marks them `status="failed"` with `error_message="heartbeat timeout"`. This is the **orphaned-process detector** that runs on the supervisor side, not FastAPI (heartbeat, not PID probing — Codex v2 P0 fix).
- **Trading subprocesses** are children of the live-supervisor container. When the supervisor container restarts, its children die. **This is accepted**: a container restart is a full node restart. Nautilus's `LiveExecEngineConfig.reconciliation = True` + `CacheConfig.database = redis` + `NautilusKernelConfig.load_state = True` reconcile broker state and rehydrate the cache on the next start. Reconciliation is fast (seconds) and the operator can choose to halt all strategies before restarting the supervisor if they want zero open positions during the gap.
- **FastAPI is never killed by a supervisor restart** — they're separate containers. `GET /status/{deployment_id}` keeps working. If the supervisor is dead, `status` will show stale heartbeats and the `/start` and `/stop` endpoints will return 503 until the supervisor is back.
- **Killing FastAPI** does not touch the supervisor or its children. The trading subprocess keeps running. The projection consumer (Phase 3) reconnects to the Redis consumer group on FastAPI restart and resumes streaming events from where it left off.

**7. Deterministic identities derived from a stable `deployment_slug`, keyed by a comprehensive `identity_signature`.**

Nautilus defaults `trader_id` to `TRADER-001` (collisions) and leaves `order_id_tag` at `None` (unstable strategy IDs — Codex v2 P1). v3 derived these from `deployment_id`, but the live_deployments model created a fresh row on every restart, breaking Phase 4 state reload (Codex v3 P1). v4 introduced a stable `deployment_slug` keyed by `(started_by, strategy_id, paper_trading, instruments_signature)` — but that tuple was too coarse (Codex v4 P0). A config change, code-hash change, account change, or strategy-class rename all reused the same `trader_id` and would have made Nautilus reload **incompatible** old state into a materially different deployment. The tuple also made two parameterizations of the same strategy on the same instruments impossible.

v5's identity model uses a **comprehensive** tuple, hashed via canonical-JSON sha256:

```python
import secrets, hashlib, json

@dataclass(slots=True, frozen=True)
class DeploymentIdentity:
    """Everything that distinguishes one logical deployment from another.

    Two deployments with the same identity_signature SHARE state across
    restarts (warm reload). Two with any different field have different
    signatures and start cold.
    """
    started_by: str            # user id (UUID hex)
    strategy_id: str           # strategy uuid hex (FK to strategies table)
    strategy_code_hash: str    # sha256 of the strategy file bytes (1.12)
    config_hash: str           # sha256 of canonical-json strategy config
    account_id: str            # IB account id (e.g. DU1234567)
    paper_trading: bool
    instruments_signature: str # sorted, comma-joined canonical instrument IDs

    def to_canonical_json(self) -> bytes:
        """Stable representation for hashing. Sort keys, no whitespace."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def signature(self) -> str:
        """sha256 hex (64 chars). The unique key for the live_deployments row."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


def compute_config_hash(config: dict) -> str:
    """sha256 of the canonical JSON of a strategy config dict."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def derive_deployment_identity(
    user_id: UUID,
    strategy_id: UUID,
    strategy_code_hash: str,
    config: dict,
    account_id: str,
    paper_trading: bool,
    instruments: list[str],
) -> DeploymentIdentity:
    return DeploymentIdentity(
        started_by=user_id.hex,
        strategy_id=strategy_id.hex,
        strategy_code_hash=strategy_code_hash,
        config_hash=compute_config_hash(config),
        account_id=account_id,
        paper_trading=paper_trading,
        instruments_signature=",".join(sorted(instruments)),
    )
```

The `identity_signature` (sha256 hex, 64 chars) is what `live_deployments` is uniquely keyed by. Lookup at `/start`:

1. Compute `identity_signature` from the request
2. `SELECT * FROM live_deployments WHERE identity_signature = :sig`
3. If found: reuse the existing `deployment_slug`, `trader_id`, etc. — warm restart
4. If not found: INSERT new row with `deployment_slug = secrets.token_hex(8)` — cold start

The 16-hex `deployment_slug` is still used for the Nautilus `trader_id`/`order_id_tag` because those have a length limit and ergonomic readability matters in logs:

```python
deployment_slug = secrets.token_hex(8)  # 16 hex chars = 64 bits
trader_id = f"MSAI-{deployment_slug}"
order_id_tag = deployment_slug
# Nautilus StrategyId becomes f"{ClassName}-{order_id_tag}"
```

**Operator UX implication (locked):** Editing any field in the strategy config produces a new `identity_signature`, a new `live_deployments` row, a new `deployment_slug`, and a cold start with no state reload. This is intentional — stale indicator state should not contaminate a tweaked strategy. Restarting the **same** strategy + config + account + instruments warm-reloads from the prior state. Two parameterizations of the same strategy file on the same instruments (e.g. EMA(10,20) and EMA(50,200) on AAPL.NASDAQ) get distinct `identity_signature`s and run as separate deployments with isolated state.

`live_node_processes` rows are still created per restart — they record the per-process lifecycle (pid, status, heartbeat). They reference `live_deployments.id` via FK. The split is: **`live_deployments` is the stable logical record; `live_node_processes` is the per-restart run record.**

**8. `stream_per_topic = False` — one Redis stream per trader.**

With `stream_per_topic = True`, Nautilus publishes to `events.order.{strategy_id}`, `events.position.{strategy_id}`, etc. — one stream per (topic, strategy). FastAPI can't subscribe before those streams exist (wildcard `XREADGROUP` is not a thing). v3 uses `stream_per_topic = False`, which produces one stream per trader: `trader-MSAI-{deployment_slug}-stream`. The stream name is deterministic and can be registered in `live_node_processes` at start time so FastAPI knows what to subscribe to.

**9. WebSocket fan-out via Redis pub/sub, not in-memory queues.**

FastAPI runs with `--workers 2`. In-memory queues live inside a single uvicorn worker, so a WebSocket client only sees events from the worker that processed them (Codex v2 P1). v3 uses a Redis pub/sub channel per deployment (`msai:live:events:{deployment_id}`). The projection consumer (one per uvicorn worker) publishes translated events to the channel; every uvicorn worker subscribes and broadcasts to its own WebSocket clients. No in-memory state shared across workers.

**10. FastAPI imports `nautilus_trader` to use the Cache Python API — but ephemerally per request, not as a long-lived Cache.**

Reading Nautilus's Redis keys directly is wrong — those names are internal implementation details. The right pattern is to build a transient `Cache` in FastAPI pointed at the same Redis backend, populate it via `cache_all()`, read from it, and dispose it. **Each request gets a fresh `Cache`.** v3 wrongly kept long-lived `Cache` instances; the `Cache` does not subscribe to Redis updates so its view drifts after `cache_all()` (Codex v3 P1). v4 corrected the import path and the lifetime model, but called the `CacheDatabaseAdapter` constructor with only `trader_id` and `config` (Codex v4 P1). v5 added `instance_id` and `serializer` but used the wrong class name and passed a string for `encoding` (Codex v5 P1). v6 uses exactly the construction Nautilus itself uses internally (`system/kernel.py:309-317`):

```python
import uuid
import msgspec

from nautilus_trader.cache.cache import Cache
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.cache.config import CacheConfig             # NOT common.config
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.identifiers import StrategyId, TraderId
from nautilus_trader.serialization.serializer import MsgSpecSerializer


def read_open_positions(redis_host: str, redis_port: int, trader_id: str, strategy_id: str) -> list[Position]:
    cfg = CacheConfig(database=DatabaseConfig(type="redis", host=redis_host, port=redis_port), encoding="msgpack")
    adapter = CacheDatabaseAdapter(
        trader_id=TraderId(trader_id),
        instance_id=UUID4(uuid.uuid4().hex),
        serializer=MsgSpecSerializer(
            encoding=msgspec.msgpack,          # MODULE, not the string "msgpack" — Codex v5 P1
            timestamps_as_str=True,            # matches how Nautilus constructs it (kernel.py:315)
            timestamps_as_iso8601=False,
        ),
        config=cfg,
    )
    try:
        cache = Cache(database=adapter)
        cache.cache_all()  # one-shot batch load — NOT a live subscription
        return cache.positions_open(strategy_id=StrategyId(strategy_id))
    finally:
        adapter.close()
```

`CacheDatabaseAdapter.__init__` (verified at `nautilus_trader/cache/database.pyx:132-138`):

```
def __init__(
    self,
    TraderId trader_id not None,
    UUID4 instance_id not None,
    Serializer serializer not None,
    config: CacheConfig | None = None,
) -> None: ...
```

`MsgSpecSerializer.__init__` (verified at `nautilus_trader/serialization/serializer.pyx:52-61`):

```
def __init__(
    self,
    encoding,                            # module (e.g. msgspec.msgpack), NOT a string
    bint timestamps_as_str = False,
    bint timestamps_as_iso8601 = False,
):
    self._encode = encoding.encode       # uses encoding.encode / encoding.decode
    self._decode = encoding.decode
```

All four `CacheDatabaseAdapter` arguments are required. The serializer must match the encoding the live trading subprocess uses (msgpack with `timestamps_as_str=True`, matching `kernel.py:315`'s `# Hardcoded for now`) so reads decode correctly.

The cost (one Redis batch read per request) is acceptable because it only runs in two places: (a) the WebSocket initial-snapshot handler on connect, and (b) PositionReader's cold path when `ProjectionState` doesn't have a deployment yet (Phase 3 task 3.5). Steady-state UI reads come from `ProjectionState` (in-memory, populated by the StateApplier task in 3.4 from the state pub/sub channel), not from Cache rebuilds.

This requires `nautilus_trader` as a runtime dep of the FastAPI backend (it already is). No raw key access.

**11. Strategies use `manage_stop = True` for native flatten.**

`StrategyConfig.manage_stop = True` tells Nautilus to close all positions and cancel all orders automatically on strategy stop. v3 uses this instead of custom `on_stop` flatten code (Codex v2 P2 — we were still reinventing).

**12. Redis Streams PEL recovery via `XAUTOCLAIM`, not "automatic redelivery".**

v3 wrongly assumed un-ACKed messages on a Redis Stream would be redelivered to a new consumer in the same group (Codex v3 P0). They are not. They sit parked in the **Pending Entries List** (PEL) for the original consumer until they are explicitly claimed by another consumer via `XCLAIM` or `XAUTOCLAIM` (Redis ≥ 6.2).

v4 makes both consumer-group users (the supervisor command bus, 1.6, and the projection consumer, 3.4) explicitly recover stale pending entries:

```python
# On consumer startup, before first XREADGROUP:
async def _recover_pending(self) -> None:
    """Reclaim entries idle longer than min_idle_time_ms.

    Run as the FIRST step in consume(): if the previous consumer
    crashed mid-message, its un-ACKed entries are now ours to retry.
    """
    cursor = "0-0"
    while True:
        cursor, claimed, _ = await self._redis.xautoclaim(
            name=self._stream,
            groupname=self._group,
            consumername=self._consumer_id,
            min_idle_time=self._min_idle_ms,
            start_id=cursor,
            count=100,
        )
        for entry_id, fields in claimed:
            yield self._decode(entry_id, fields)
        if cursor == "0-0":
            break
```

After `_recover_pending()`, the consumer enters its normal `XREADGROUP BLOCK ...` loop. On every iteration the consumer also calls `_recover_pending()` periodically (e.g. every 30 seconds) to handle the case where a peer consumer crashes mid-flight in steady state.

**13. Idempotency at every layer.**

v3 had no idempotency. A retried `/start` after a 504 timeout, or a supervisor crash between `process.start()` and the Redis ACK, both produced duplicate trading subprocesses with the same deterministic identity (Codex v3 P0). v4 adds three layers:

- **Database layer:** A unique partial index on `live_node_processes(deployment_id)` `WHERE status IN ('starting','building','ready','running','stopping')`. Two concurrent spawns for the same `deployment_id` cannot both insert active rows.
- **Supervisor layer:** Before spawning, the supervisor runs `SELECT ... FOR UPDATE SKIP LOCKED` on the `live_deployments` row by `deployment_slug`, then checks `live_node_processes` for an active row. If one exists, the spawn is a no-op and the command is ACKed (idempotent success). If not, it inserts and spawns. The command is ACKed **only after** the spawn is observed to succeed (`live_node_processes.status` reached `building` or later) — NOT in `finally`.
- **API layer:** `/api/v1/live/start` accepts an `Idempotency-Key` HTTP header (RFC draft). The key is hashed to a Redis key `msai:idem:start:{hash}` with the result and a 24-hour TTL. A retry with the same key returns the cached response immediately and does NOT publish a new command. Without an idempotency key, the API still de-duplicates by checking if an active deployment exists for `(strategy_id, instruments_set)` and returns the existing one.

**14. Post-start health check via the canonical FSM signal `kernel.trader.is_running`.**

v3 treated `kernel.start_async()` returning as proof of readiness (Codex v3 P0). v4 added a polling check, but the conditions it polled (`data_engine.is_connected`, `exec_engine.is_connected`, engine-level `reconciliation_active`) **don't exist** as attributes in Nautilus 1.223.0 — Codex v4 P1.

**Verified against `nautilus_trader/system/kernel.py:1001-1037`:** `start_async` runs through `_await_engines_connected()` → `_await_execution_reconciliation()` → `_await_portfolio_initialization()` → `self._trader.start()`. Each await silently early-returns on failure (returns `False`, no exception). The trader's FSM transitions to RUNNING **only inside `_trader.start()` on the last line** — which is reached only on full success of every prior await.

So the canonical "fully started" signal is `kernel.trader.is_running` (a property on `Trader`/`Component` that reads the FSM state). If True after `start_async` returns, every internal wait succeeded. If False, one of them silently returned early.

v5's post-start health check is a single condition with a structured diagnose helper:

```python
from time import monotonic
import asyncio

async def wait_until_ready(node: "TradingNode", timeout_s: int = 60) -> None:
    """After kernel.start_async() returns, verify the trader actually started.

    Polls kernel.trader.is_running. Raises StartupHealthCheckFailed with a
    structured diagnosis on timeout.
    """
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        if node.kernel.trader.is_running:
            return
        await asyncio.sleep(0.5)
    raise StartupHealthCheckFailed(diagnose(node))


def diagnose(node: "TradingNode") -> str:
    """Build a structured failure-reason string using the REAL Nautilus accessors."""
    kernel = node.kernel
    parts = [f"trader.is_running={kernel.trader.is_running}"]

    # Engine connectivity uses METHODS, not attributes
    parts.append(f"data_engine.check_connected()={kernel.data_engine.check_connected()}")
    parts.append(f"exec_engine.check_connected()={kernel.exec_engine.check_connected()}")

    # reconciliation_active is per-LiveExecutionClient (live/execution_client.py:136),
    # not per-engine. Iterate the registered exec clients:
    for client in kernel.exec_engine.registered_clients.values():
        parts.append(f"{client.id}.reconciliation_active={getattr(client, 'reconciliation_active', None)}")

    parts.append(f"portfolio.initialized={kernel.portfolio.initialized}")
    parts.append(f"cache.instruments_count={len(kernel.cache.instruments())}")

    return "; ".join(parts)


class StartupHealthCheckFailed(Exception):
    pass
```

The trading subprocess catches `StartupHealthCheckFailed` in its outer try/except, writes `live_node_processes.status="failed"` with the diagnosis as `error_message`, then exits cleanly via the `finally` block (which still runs `node.dispose()`).

Only after `wait_until_ready` returns successfully does the subprocess write `status="ready"`. The `/start` API (1.14) polls for `ready` OR `failed` and returns the right HTTP status accordingly.

**15. Supervisor keeps an in-memory `dict[deployment_id, mp.Process]`.**

v3 threw away the `mp.Process` handles immediately after `process.start()` and relied solely on heartbeat staleness for liveness (Codex v3 P2). That works for cross-container recovery but loses **instant** in-process exit detection: the supervisor can't reap zombies, can't observe real exit codes, and can't detect a crash for up to 30 seconds.

The parent (supervisor) and child (trading subprocess) are in the **same** Linux namespace (same container), so `Process.is_alive()` and `Process.exitcode` ARE meaningful. v4 keeps the handle map:

```python
class ProcessManager:
    def __init__(self, ...) -> None:
        self._handles: dict[UUID, mp.Process] = {}  # in-memory, supervisor-local
        ...

    async def _reap_loop(self) -> None:
        """Background task: poll handles for exits, surface real exit codes."""
        while not self._stop_event.is_set():
            for deployment_id, proc in list(self._handles.items()):
                if not proc.is_alive():
                    await self._on_child_exit(deployment_id, proc.exitcode)
                    del self._handles[deployment_id]
            await asyncio.sleep(1)
```

Heartbeat freshness is still the **recovery/discovery** signal — when the supervisor itself restarts, the in-memory map is empty and we have to rediscover via the database. But for normal in-process operation, the handle map is faster, more accurate, and surfaces real exit codes.

**16. Kill switch is push-based, not bar-poll.**

v3's "strategy reads `msai:risk:halt` flag on every `on_bar`" allowed up to one bar of lag — for a 1-minute bar that's a 60-second lag on an emergency halt (Codex v3 P2). v4 makes `/api/v1/live/kill-all` a push:

1. Set `msai:risk:halt = true` in Redis with a long TTL (persistence — survives any restart)
2. For every `live_node_processes` row with `status IN ('ready','running')`, publish a `stop` command to the supervisor's command stream with `reason="kill_switch"` and a special flag that requests immediate flatten (the supervisor SIGTERMs the child, `manage_stop=True` flattens automatically)
3. The pre-submit risk check in `RiskAwareStrategy` is the **third layer of defense**: it reads the halt flag from the cached value updated by an async task on every `on_bar` (still bar-cadence). This is defense-in-depth, NOT the primary mechanism.

Latency from operator click to flatten: bounded by the `XADD` latency to the supervisor command stream + the supervisor's `XREADGROUP BLOCK 5000` window + SIGTERM delivery + Nautilus stop. Realistically < 5 seconds. Compared to v3's "up to one bar," this is a 12× improvement on a 1m bar and 720× on a 1h bar.

`POST /api/v1/live/resume` clears `msai:risk:halt` and is required before `/start` will accept new deployments. There is no auto-resume.

**17. Single startup-liveness authority: the Watchdog. HeartbeatMonitor scans post-startup statuses only.**

`node.build()` (verified at `live/node.py:272-281`) constructs IB data clients and exec clients. The IB client builder may issue contract-detail fetches, which can hang on network failures (Codex v4 P1). v4 started the heartbeat AFTER `node.build()` and excluded `'building'` from the stale sweep — a hung build wedged the deployment forever.

v5 tried to fix that by starting the heartbeat before `node.build()` and including `'starting'`+`'building'` in the stale sweep. v6 added a supervisor-side watchdog for process-level SIGKILL. Codex v6 P0 then found that the two overlapped in a harmful way: the heartbeat monitor's 30s stale sweep flipped the row to `failed` BEFORE the watchdog's 180s wall-clock deadline, so the watchdog query no longer matched, the slot freed, `/stop` and `/kill-all` lost the row, and **the real wedged process was still running** — a retry could spawn a duplicate child.

v7's rule is strict: **during `starting`/`building`, only the Watchdog may mark a row as `failed`, and only after it has already SIGKILLed the process**. The HeartbeatMonitor scans `'ready'`, `'running'`, and `'stopping'` only — it never touches startup statuses.

- **Trading subprocess (1.8):** unchanged from v6 — start the heartbeat thread immediately after writing `pid=os.getpid()` and `status='building'`, BEFORE `node.build()`. Heartbeat advances every 5 seconds throughout build. A slow-but-healthy build keeps the heartbeat fresh; the watchdog sees progress and does not kill.
- **Watchdog (1.7 `ProcessManager.watchdog_loop`):** heartbeat-based kill condition instead of wall-clock. SELECT rows where `status IN ('starting','building') AND last_heartbeat_at < now() - stale_seconds` (default 30s). For each match: SIGKILL the pid, then flip the row to `failed` in the SAME transaction (so there's no window where the row is out of the active set but the process is still alive). A secondary hard wall-clock backstop at `started_at < now() - default_startup_hard_timeout_s` (default 1800s in v8, was 600s in v7 — Codex v7 P2) catches pathological degenerate-loop cases where the heartbeat thread somehow stays alive but the process is hosed. Per-deployment override via `live_deployments.startup_hard_timeout_s` (nullable — NULL falls back to the supervisor default).
- **HeartbeatMonitor (1.7, v7):** stale-sweep query is narrowed to `status IN ('ready','running','stopping')`. This is the post-startup orphan detector — for cross-supervisor-restart discovery of deployments that were running but lost their parent. Startup statuses are the watchdog's exclusive domain.

Combined, a wedged build is detected within `stale_seconds` (default 30s). A slow-but-healthy build taking 60–300 seconds is NOT falsely killed because its heartbeat keeps advancing. Only the single `watchdog_loop` can flip a startup row to `failed`, eliminating the race between two writers.

**Why heartbeat-based and not wall-clock:** `docs/nautilus-reference.md:482,513` warns that IB contract loading takes 10-50s per 100 instruments and options chains take 10-30s each. A deployment with 30 options underlyings can legitimately need > 180s in build. v6's wall-clock deadline would have killed legitimate slow builds. v7's heartbeat-based deadline kills only when the subprocess stops making any progress.

**18. Each phase ends with a docker-based E2E test** that exercises the actual subprocess lifecycle, IB Gateway, Postgres, Redis, and (where relevant) the frontend.

---

## Phase 1 — Live Node + Live Supervisor + Audit

**Goal:** Claude can launch a real Nautilus `TradingNode` against IB Gateway paper, supervised by a dedicated `live-supervisor` Docker service, with deployment registry, structured logging, order audit, and a deterministic E2E that proves the order path.

**Phase 1 acceptance:**

- `POST /api/v1/live/start` publishes a command to the Redis stream (`msai:live:commands`)
- The `live-supervisor` service (its own Docker container) consumes the command via `XREADGROUP` and spawns a real `TradingNode` subprocess as a child of its own container
- Subprocess builds a `TradingNode` with deterministic `trader_id=f"MSAI-{deployment_slug}"` and `order_id_tag=deployment_slug`, connects to IB Gateway paper, completes reconciliation inside `kernel.start_async()`, transitions to `status="ready"` immediately after
- The deterministic smoke strategy submits a tiny AAPL market order on the first bar
- The order is recorded in `order_attempt_audits` with `client_order_id`, then updated through accepted/filled
- Killing the FastAPI container has zero effect on the trading subprocess (the supervisor and its children are in a different container)
- After API restart, `GET /api/v1/live/status/{deployment_id}` finds the surviving subprocess via the `live_node_processes` table
- `POST /api/v1/live/stop` publishes a stop command, the supervisor sends `SIGTERM`, the subprocess's `manage_stop = True` native flatten cancels orders + closes positions automatically, `node.stop_async()` and `dispose()` run in the `finally` block, exits cleanly
- Heartbeat freshness (not cross-container PID probing) is the sole liveness signal: the supervisor's `HeartbeatMonitor` marks rows with stale `last_heartbeat_at` as `status="failed"`

### Phase 1 tasks

#### 1.1 — `live_node_processes` table + model

Files:

- `claude-version/backend/src/msai/models/live_node_process.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_live_node_processes.py` (new)
- `claude-version/backend/tests/integration/test_live_node_process_model.py` (new)

```python
class LiveNodeProcess(Base, TimestampMixin):
    __tablename__ = "live_node_processes"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(ForeignKey("live_deployments.id"), index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # NULLABLE because the row is inserted with status="starting" BEFORE
    # process.start() returns a pid (Codex v3 P1 fix). The supervisor
    # updates pid to the real value after spawn.
    host: Mapped[str] = mapped_column(String(255), nullable=False)  # docker container hostname
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # values: starting | building | ready | running | stopping | stopped | failed
    # 'building' is written by the subprocess during kernel.build() — Codex v3 P1 added.
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # v7 addition (Codex v6 P1): structured failure classification.
    # StrEnum values: none | halt_active | spawn_failed_permanent |
    # reconciliation_failed | build_timeout | api_poll_timeout.
    # The /api/v1/live/start endpoint reads this column (not the
    # error_message string) to decide whether the EndpointOutcome
    # should be cacheable. See idempotency.py FailureKind.

    __table_args__ = (
        # Idempotency layer (decision #13): a deployment can have at most ONE
        # active process row at any time. Two concurrent spawns racing on the
        # same deployment_id will fail at the database with a uniqueness
        # violation, which the supervisor catches and treats as "already
        # active, ACK the command."
        Index(
            "uq_live_node_processes_active_deployment",
            "deployment_id",
            unique=True,
            postgresql_where=text(
                "status IN ('starting','building','ready','running','stopping')"
            ),
        ),
    )
```

TDD:

1. Model integration test: create a row, query it back
2. Insert two rows with the same `deployment_id` and `status='ready'`, assert the second insert raises `IntegrityError`
3. Insert two rows with the same `deployment_id` where one is `status='stopped'` and the other is `status='ready'`, assert both succeed
4. Insert a row with `pid=None`, assert success (column is nullable)
5. Insert a row with `status='building'`, assert success
6. Then write the model + migration

Acceptance: all six tests pass; `alembic upgrade head` succeeds on a fresh database.

Effort: S
Depends on: nothing
Gotchas: Codex v3 P1 (nullable pid, building status, partial unique index for idempotency)

---

#### 1.1b — `live_deployments` schema migration (broader stable identity)

Files:

- `claude-version/backend/src/msai/models/live_deployment.py` (modify)
- `claude-version/backend/src/msai/services/live/deployment_identity.py` (new)
- `claude-version/backend/alembic/versions/<rev>_live_deployments_stable_identity.py` (new)
- `claude-version/backend/tests/unit/test_deployment_identity.py` (new)
- `claude-version/backend/tests/integration/test_live_deployment_stable_identity.py` (new)

The existing model docstring says "A new deployment row is created each time a strategy is (re-)started." That contradicts the stable-identity model (decision #7) and breaks Phase 4 state reload (Codex v3 P1). v4's first attempt keyed the stable identity by `(user, strategy, instruments, paper_trading)`, but that was too coarse — Codex v4 P0 found that a config change, code-hash change, or account change would silently reuse the same `trader_id`. v5 uses a comprehensive, hashed identity tuple (decision #7).

New columns added by this migration:

```python
class LiveDeployment(Base):
    """A live or paper-trading deployment of a strategy.

    A deployment is a STABLE logical record uniquely keyed by
    identity_signature (a sha256 of the canonical-JSON of the broader
    identity tuple — see services/live/deployment_identity.py).

    Two deployments with the same identity_signature SHARE state
    across restarts (warm reload). Two with any different field have
    different signatures and start cold.

    Per-restart per-process state lives in live_node_processes.
    """

    # ... existing columns above ...

    deployment_slug: Mapped[str] = mapped_column(String(16), nullable=False, unique=True, index=True)
    # 16 hex chars = 64 bits, derived from secrets.token_hex(8) at first
    # creation. Used to derive trader_id, order_id_tag, and the Nautilus
    # message bus stream name. Stable across restarts of the same identity.

    identity_signature: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # sha256 hex of the canonical-JSON identity tuple (decision #7).
    # Includes: started_by, strategy_id, strategy_code_hash, config_hash,
    # account_id, paper_trading, instruments_signature.
    # The UNIQUE constraint enforces "warm restart on exact match, cold
    # start on any change."

    trader_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # f"MSAI-{deployment_slug}" — convenience denormalization for log queries.

    strategy_id_full: Mapped[str] = mapped_column(String(64), nullable=False)
    # f"{strategy_class_name}-{deployment_slug}" — the Nautilus StrategyId.value

    account_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # IB account id (e.g. "DU1234567" for paper, "U1234567" for live).
    # Also part of the identity tuple — changing accounts produces a new
    # deployment row.

    message_bus_stream: Mapped[str] = mapped_column(String(96), nullable=False)
    # f"trader-MSAI-{deployment_slug}-stream" — the deterministic Redis
    # stream name where Nautilus publishes events for this trader
    # (Phase 3 task 3.2). Persisted here so the projection consumer
    # knows what to subscribe to without polling Redis for stream names.

    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256 hex of the canonical JSON of the strategy config dict.
    # Persisted for diagnostics — the source of truth is the identity
    # tuple, but having the hash on the row makes log triage trivial.

    instruments_signature: Mapped[str] = mapped_column(String(512), nullable=False)
    # Sorted, comma-joined canonical instrument IDs. Same format as the
    # identity tuple field, persisted for diagnostics.

    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    startup_hard_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v8 addition (Codex v7 P2): per-deployment override for the
    # supervisor watchdog's hard wall-clock ceiling. NULL → use the
    # supervisor default (1800s in v8, up from 600s in v7). Operators
    # with large options universes (30+ underlyings, 10000+ strikes)
    # can raise this per deployment. The watchdog's HEARTBEAT-based
    # primary condition is independent of this value — a subprocess
    # whose heartbeat thread keeps advancing is never killed regardless
    # of this timeout. This is only the secondary "degenerate loop"
    # backstop.
    # Replace the old started_at/stopped_at — those tracked the FIRST
    # start, but a deployment can be (re-)started many times.

    # NOTE: no separate composite unique index — UNIQUE(identity_signature)
    # is the single source of identity truth.
```

The migration:

1. Adds the new columns with default placeholders for any pre-existing rows
2. Backfills `deployment_slug` for existing rows via `secrets.token_hex(8)` per-row
3. Backfills `trader_id`, `strategy_id_full`, `message_bus_stream` from the slug
4. Backfills `account_id` from the IBSettings env vars (stamped at migration time as a best-effort)
5. Backfills `instruments_signature` by sorting and joining `instruments`
6. Backfills `config_hash` from the row's `config` JSONB column
7. Backfills `identity_signature` by computing the sha256 of the broader tuple
8. Backfills `last_started_at` from `started_at`, `last_stopped_at` from `stopped_at`
9. Creates the unique index on `identity_signature`
10. Drops the old `started_at` / `stopped_at` columns

`deployment_identity.py`:

```python
"""Identity computation for stable deployment identification.

The identity_signature is the sha256 of the canonical-JSON of the
DeploymentIdentity dataclass — see decision #7 in the plan."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from uuid import UUID


@dataclass(slots=True, frozen=True)
class DeploymentIdentity:
    started_by: str
    strategy_id: str
    strategy_code_hash: str
    config_hash: str
    account_id: str
    paper_trading: bool
    instruments_signature: str

    def to_canonical_json(self) -> bytes:
        return json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def signature(self) -> str:
        """64-char sha256 hex — the unique identity_signature for the row."""
        return hashlib.sha256(self.to_canonical_json()).hexdigest()


def compute_instruments_signature(instruments: list[str]) -> str:
    return ",".join(sorted(instruments))


def compute_config_hash(config: BaseModel | dict) -> str:
    """sha256 of the canonical-JSON representation of the strategy config.

    v6 change (Codex v5 P3): accepts a Pydantic BaseModel as the primary
    input. The model is dumped via model_dump(mode="json") first, which
    applies type coercion and defaults. Semantically-identical configs
    (e.g. {"x": 5} and {"x": "5"} if x is int) then produce the same
    hash. Dict inputs are accepted as a convenience (tests, migrations)
    but the endpoint path always passes the validated model.
    """
    if isinstance(config, BaseModel):
        normalized = config.model_dump(mode="json")
    else:
        normalized = config
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def generate_deployment_slug() -> str:
    """16 hex chars = 64 bits. Stable per identity, NOT per row UUID."""
    return secrets.token_hex(8)


def derive_trader_id(slug: str) -> str:
    return f"MSAI-{slug}"


def derive_strategy_id_full(strategy_class_name: str, slug: str) -> str:
    return f"{strategy_class_name}-{slug}"


def derive_message_bus_stream(slug: str) -> str:
    return f"trader-{derive_trader_id(slug)}-stream"


def derive_deployment_identity(
    user_id: UUID,
    strategy_id: UUID,
    strategy_code_hash: str,
    config: dict,
    account_id: str,
    paper_trading: bool,
    instruments: list[str],
) -> DeploymentIdentity:
    return DeploymentIdentity(
        started_by=user_id.hex,
        strategy_id=strategy_id.hex,
        strategy_code_hash=strategy_code_hash,
        config_hash=compute_config_hash(config),
        account_id=account_id,
        paper_trading=paper_trading,
        instruments_signature=compute_instruments_signature(instruments),
    )
```

TDD:

1. Migration test: starting with the existing schema, run the upgrade, verify all new columns exist with correct types
2. Migration test: insert a pre-existing row before the migration, run the migration, verify the row has a backfilled `identity_signature`, `deployment_slug`, and all derived columns
3. **Identity uniqueness test**: insert two `LiveDeployment` rows with the same `identity_signature`, assert the second raises `IntegrityError`
4. **Cold-start test**: derive identity for the same strategy with two DIFFERENT `config` dicts → assert two different `identity_signature`s → both inserts succeed
5. **Cold-start test**: derive identity for two different `strategy_code_hash` values (e.g., the strategy file was edited) → two different signatures
6. **Cold-start test**: same strategy + config but different `account_id` → two different signatures
7. **Warm-start test**: derive identity twice with identical inputs → same `identity_signature`
8. Helper test: `compute_instruments_signature(["MSFT.NASDAQ", "AAPL.NASDAQ"]) == "AAPL.NASDAQ,MSFT.NASDAQ"` (sorted)
9. Helper test: `derive_message_bus_stream("a1b2c3d4e5f60718") == "trader-MSAI-a1b2c3d4e5f60718-stream"`
10. Helper test: `compute_config_hash({"x": 1, "y": 2}) == compute_config_hash({"y": 2, "x": 1})` (canonical JSON sorts keys)
11. **Validated-model hash test (Codex v5 P3)**: define a Pydantic model `StratConfig(x: int)`, construct `a = StratConfig(x=5)` and `b = StratConfig(x="5")` (Pydantic coerces the string to int), assert `compute_config_hash(a) == compute_config_hash(b)`. Raw-dict equivalents `{"x": 5}` and `{"x": "5"}` should hash DIFFERENTLY — verifying that the caller passes the validated model, not the raw dict
12. Implement

Acceptance: all twelve tests pass; `alembic upgrade head` succeeds on a fresh database AND on a database with pre-existing rows.

Effort: M
Depends on: 1.1
Gotchas: Codex v4 P0 (broader identity tuple — code_hash, config_hash, account_id all in the signature), Codex v5 P3 (hash the Pydantic-validated model, not the raw dict), decision #7

---

#### 1.2 — `order_attempt_audits` table + model with `client_order_id`

Files:

- `claude-version/backend/src/msai/models/order_attempt_audit.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_order_attempt_audit.py` (new)
- `claude-version/backend/tests/integration/test_order_attempt_audit_model.py` (new)

```python
class OrderAttemptAudit(Base, TimestampMixin):
    __tablename__ = "order_attempt_audits"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, unique=True)
    deployment_id: Mapped[UUID | None] = mapped_column(ForeignKey("live_deployments.id"), index=True, nullable=True)
    backtest_id: Mapped[UUID | None] = mapped_column(ForeignKey("backtests.id"), index=True, nullable=True)
    strategy_id: Mapped[UUID] = mapped_column(ForeignKey("strategies.id"), index=True, nullable=False)
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_attempted: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # values: submitted | accepted | rejected | filled | partially_filled | cancelled | denied
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        CheckConstraint("(deployment_id IS NOT NULL) OR (backtest_id IS NOT NULL)"),
    )
```

The `client_order_id` is the correlation key. The audit hook in 1.11 generates this UUID, writes the initial `submitted` row, and looks the row up by `client_order_id` to update through accepted → filled.

TDD: integration test creates a row, updates via `client_order_id`, asserts state machine.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex finding #7 — `client_order_id` is the stable correlation key

---

#### 1.3 — Structured logging with `deployment_id` context

Files:

- `claude-version/backend/src/msai/core/logging.py` (modify)
- `claude-version/backend/tests/unit/test_logging.py` (extend)

Add a `deployment_id` context variable injected into every structlog record. Add `bind_deployment(deployment_id)` context manager.

TDD: test that `with bind_deployment(uuid)` causes subsequent log calls to include `deployment_id`.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: none

---

#### 1.4 — Minimal real instrument bootstrap (NOT `TestInstrumentProvider`)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py` (new)
- `claude-version/backend/tests/unit/test_live_instrument_bootstrap.py` (new)

Returns an `InteractiveBrokersInstrumentProviderConfig` with `load_contracts` populated for the Phase 1 paper symbols. Phase 2 replaces this with the full SecurityMaster.

```python
_PHASE_1_PAPER_SYMBOLS = {
    "AAPL": IBContract(secType="STK", symbol="AAPL", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
    "MSFT": IBContract(secType="STK", symbol="MSFT", exchange="SMART", primaryExchange="NASDAQ", currency="USD"),
}

def build_ib_instrument_provider_config(symbols: list[str]) -> InteractiveBrokersInstrumentProviderConfig:
    contracts = frozenset(_PHASE_1_PAPER_SYMBOLS[s] for s in symbols)
    return InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        load_contracts=contracts,
        cache_validity_days=1,
    )
```

TDD: test that `build_ib_instrument_provider_config(["AAPL"])` returns a config with the right contract; unknown symbol raises `ValueError`.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #9 (instrument not pre-loaded), #11 (don't load on critical path)

---

#### 1.5 — `build_live_trading_node_config()` builder

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (new)
- `claude-version/backend/tests/unit/test_live_node_config.py` (new)

```python
def build_live_trading_node_config(
    deployment_id: UUID,
    strategy_path: str,
    strategy_config: dict,
    paper_symbols: list[str],
    ib_settings: IBSettings,
) -> TradingNodeConfig:
    """Build the TradingNodeConfig used by the live trading subprocess.

    Uses Nautilus natives for everything Nautilus already provides:
    - LiveDataEngineConfig — defaults
    - LiveExecEngineConfig — reconciliation=True (default), reconciliation_lookback_mins=1440
    - LiveRiskEngineConfig — bypass=False, max_notional_per_order populated from deployment
    - InteractiveBrokersDataClientConfig — instrument provider from build_ib_instrument_provider_config
    - InteractiveBrokersExecClientConfig — paper port (4002), account_id from settings
    - cache and message_bus left UNCONFIGURED in Phase 1 (Phase 3 adds Redis)
    - load_state and save_state left at default False in Phase 1 (Phase 4 enables them)
    - strategies = [ImportableStrategyConfig(strategy_path=...)]

    Each call gets a unique ibg_client_id per deployment so concurrent
    deployments don't collide (gotcha #3). Uses ib_data_client_id and
    ib_exec_client_id (separate IDs) to avoid the data/exec collision.

    Validation:
    - paper_symbols must be non-empty
    - port=4002 implies account_id starts with "DU" (paper); port=4001 implies it doesn't (live)
    """
```

TDD:

1. Happy path
2. Each validation rejection
3. Two calls with different deployment IDs produce different `ibg_client_id` values
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.4
Gotchas: #3 (client_id collision), #6 (port/account mismatch)

---

#### 1.6 — Redis command stream with PEL recovery (XAUTOCLAIM) + DLQ

Files:

- `claude-version/backend/src/msai/services/live_command_bus.py` (new)
- `claude-version/backend/tests/integration/test_live_command_bus.py` (new)
- `claude-version/backend/tests/integration/test_live_command_bus_dlq.py` (new)

```python
LIVE_COMMAND_STREAM = "msai:live:commands"
LIVE_COMMAND_GROUP = "live-supervisor"
LIVE_COMMAND_DLQ_STREAM = "msai:live:commands:dlq"
MAX_DELIVERY_ATTEMPTS = 5

class LiveCommandBus:
    """Thin wrapper over Redis Streams for the API ↔ supervisor control plane.

    Two safety mechanisms:

    1. PEL recovery via XAUTOCLAIM (decision #12, Codex v3 P0).
       Un-ACKed entries sit in the PEL until explicitly claimed.
       _recover_pending() runs on consume() startup and every
       recovery_interval_s in steady state.

    2. Poison-message DLQ via delivery-count cap (Codex v4 P2).
       Each XAUTOCLAIM returns the per-entry delivery count. Entries
       reaching MAX_DELIVERY_ATTEMPTS are moved to the DLQ stream
       (msai:live:commands:dlq) with a dlq_reason field, then XACKed
       on the original stream so they don't bounce forever.
    """

    def __init__(
        self,
        redis: Redis,
        stream: str = LIVE_COMMAND_STREAM,
        group: str = LIVE_COMMAND_GROUP,
        dlq_stream: str = LIVE_COMMAND_DLQ_STREAM,
        max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
        min_idle_ms: int = 30_000,
        recovery_interval_s: int = 30,
    ) -> None: ...

    async def publish_start(self, deployment_id: UUID, payload: dict) -> str:
        """Publish a start command. Returns the Redis stream entry ID.

        Adds an `idempotency_key` field to the payload (the caller's
        Idempotency-Key header value, or a hash of the deployment_id).
        Used by the supervisor to deduplicate retries (decision #13).
        """

    async def publish_stop(self, deployment_id: UUID, reason: str = "user") -> str:
        """Publish a stop command."""

    async def ensure_group(self) -> None:
        """Idempotently create the consumer group via XGROUP CREATE MKSTREAM."""

    async def consume(self, consumer_id: str, stop_event: asyncio.Event) -> AsyncIterator[LiveCommand]:
        """Consume from the stream as part of LIVE_COMMAND_GROUP.

        Lifecycle per call to consume():
        1. ensure_group() — idempotent XGROUP CREATE MKSTREAM
        2. _recover_pending() — XAUTOCLAIM stale entries from any
           crashed peer (or our own previous run); yield each one
           (or send to DLQ if delivery_count >= max_delivery_attempts)
        3. Enter the steady-state XREADGROUP BLOCK 5000 COUNT N loop
        4. Every recovery_interval_s, call _recover_pending() again to
           handle peers crashing in steady state
        5. Each yielded LiveCommand has an `entry_id` the caller MUST
           pass back to ack(entry_id) — explicit ack semantics, no
           auto-ack-on-yield. The caller decides when to ACK (decision
           #13: ACK only on success, never in finally).
        """

    async def ack(self, entry_id: str) -> None:
        """XACK the entry. Call only after the command has been
        successfully handled and observed in the database (e.g.,
        live_node_processes row reached 'building' or later)."""

    async def _recover_pending(self, consumer_id: str) -> AsyncIterator[LiveCommand]:
        """Reclaim entries from peers that have been idle longer than
        min_idle_ms. Yields entries that are still under the delivery
        budget; sends entries OVER the budget to the DLQ.

        cursor = "0-0"
        while True:
            cursor, claimed, _ = await redis.xautoclaim(
                name=self._stream, groupname=self._group,
                consumername=consumer_id,
                min_idle_time=self._min_idle_ms,
                start_id=cursor,
                count=100,
                justid=False,  # we need the field data
            )
            for entry_id, fields in claimed:
                # Check delivery count via XPENDING (XAUTOCLAIM increments
                # the delivery counter; we read it back to decide DLQ).
                pending = await self._redis.xpending_range(
                    name=self._stream, groupname=self._group,
                    min=entry_id, max=entry_id, count=1,
                )
                delivery_count = pending[0]["times_delivered"] if pending else 1
                if delivery_count >= self._max_delivery_attempts:
                    await self._move_to_dlq(entry_id, fields, delivery_count, reason="max_attempts")
                    continue
                yield LiveCommand.from_redis(entry_id, fields)
            if cursor == "0-0":
                break

    async def _move_to_dlq(
        self,
        entry_id: str,
        fields: dict,
        delivery_count: int,
        reason: str,
    ) -> None:
        '''Add the original entry to the DLQ stream and ACK it on the
        primary stream so it stops bouncing. Emits an alert.

        DLQ entry preserves all original fields plus diagnostic metadata:
        - original_entry_id
        - delivery_count
        - dlq_reason
        - moved_at
        '''
        await self._redis.xadd(
            self._dlq_stream,
            {
                **fields,
                "original_entry_id": entry_id,
                "delivery_count": str(delivery_count),
                "dlq_reason": reason,
                "moved_at": utcnow().isoformat(),
            },
        )
        await self._redis.xack(self._stream, self._group, entry_id)
        logger.error(
            "command_moved_to_dlq",
            entry_id=entry_id,
            delivery_count=delivery_count,
            reason=reason,
        )
        # Alerting service hook (existing service from Phase 1.3 logging)
        alert_service.fire("live_command_dlq", entry_id=entry_id, reason=reason)
```

TDD:

1. Integration test against testcontainers Redis: publish 3 commands, consume + ACK each, verify they don't reappear on next consume
2. Integration test: publish a command, consume it WITHOUT ACKing (simulating a crash), call consume again from the SAME consumer_id with `min_idle_ms=0` — verify the pending entry is yielded
3. Integration test: publish a command, consume from `consumer_a` without ACKing, then call consume from `consumer_b` (different consumer_id) with `min_idle_ms=0` — verify `consumer_b` reclaims and yields the pending entry via `_recover_pending`
4. Integration test: verify that without `_recover_pending`, the entry would NOT be auto-redelivered (sanity check that the recovery is necessary)
5. **DLQ poison-message test**: publish a command, consume + skip ACK 5 times in a row (simulating a poison message that crashes the handler), assert on the 5th XAUTOCLAIM the entry is moved to `msai:live:commands:dlq` with the original payload + `dlq_reason="max_attempts"` AND the entry is XACKed on the primary stream so it doesn't bounce again
6. **DLQ alert fires test**: mock the alerting service, trigger a DLQ move, assert `alert_service.fire` was called
7. Test the idempotency_key field is preserved through publish → consume → DLQ
8. Test that `ack` outside the yielded entry does not crash
9. Implement

Acceptance: tests pass; the documentation explicitly notes "un-ACKed entries are NOT auto-redelivered, see \_recover_pending" and "poison messages are sent to the DLQ after MAX_DELIVERY_ATTEMPTS attempts."

Effort: M
Depends on: nothing
Gotchas: Codex v3 P0 (PEL semantics — explicit XAUTOCLAIM, not auto-redelivery), Codex v4 P2 (DLQ for poison messages — bounded delivery attempts)

---

#### 1.7 — Dedicated `live-supervisor` Docker service

Files:

- `claude-version/backend/src/msai/live_supervisor/__init__.py` (new)
- `claude-version/backend/src/msai/live_supervisor/__main__.py` (new)
- `claude-version/backend/src/msai/live_supervisor/main.py` (new — the supervisor loop)
- `claude-version/backend/src/msai/live_supervisor/process_manager.py` (new — mp.Process lifecycle)
- `claude-version/backend/src/msai/live_supervisor/heartbeat_monitor.py` (new — orphaned-process detector)
- `claude-version/docker-compose.dev.yml` (add service)
- `claude-version/docker-compose.prod.yml` (add service)
- `claude-version/backend/tests/integration/test_live_supervisor.py` (new)

The supervisor runs as a standalone Python service (`python -m msai.live_supervisor`) in its own Docker container. It does NOT run inside the arq worker because arq awaits `on_startup` completion before entering its poll loop (Codex v2 P0).

`live_supervisor/main.py`:

```python
async def run_forever() -> None:
    """Main supervisor loop.

    Consumes commands from msai:live:commands via a Redis consumer
    group with explicit XAUTOCLAIM-based PEL recovery (decision #12).
    Maintains an in-memory dict[deployment_id, mp.Process] handle map
    (decision #15) and a background reap loop that surfaces real exit
    codes the moment a child dies.

    Runs until SIGTERM. On shutdown:
    - stop consuming new commands
    - drain in-flight handlers
    - do NOT send SIGTERM to any running trading subprocesses —
      they're owned by this container's OS and will be reaped when
      the container exits. The next supervisor start re-discovers
      surviving subprocesses via heartbeat-fresh rows.
    """
    bus = LiveCommandBus(redis=get_redis())
    process_manager = ProcessManager(db=async_session_factory)
    heartbeat_monitor = HeartbeatMonitor(db=async_session_factory, stale_seconds=30)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

    monitor_task = asyncio.create_task(heartbeat_monitor.run_forever(stop_event))
    reap_task = asyncio.create_task(process_manager.reap_loop(stop_event))

    try:
        async for command in bus.consume("supervisor-1", stop_event):
            ok = False
            try:
                if command.action == "start":
                    ok = await process_manager.spawn(
                        deployment_id=command.deployment_id,
                        deployment_slug=command.deployment_slug,
                        payload=command.payload,
                        idempotency_key=command.idempotency_key,
                    )
                elif command.action == "stop":
                    ok = await process_manager.stop(command.deployment_id, reason=command.reason)
                else:
                    logger.warning("unknown_command", action=command.action)
                    ok = True  # ACK so we don't loop forever on a malformed command
            except Exception as exc:
                logger.exception("command_failed", entry_id=command.entry_id, error=str(exc))
                ok = False  # leave it in the PEL for XAUTOCLAIM-based retry

            # Decision #13: ACK only on success. Failures stay in the PEL
            # so a future _recover_pending() sweep retries them.
            if ok:
                await bus.ack(command.entry_id)
    finally:
        monitor_task.cancel()
        reap_task.cancel()
```

`process_manager.py`:

```python
class ProcessManager:
    """Owns the trading subprocesses spawned by this supervisor instance.

    INSERT-spawn-UPDATE pattern (decision #13, Codex v4 P0):

    The spawn() method does NOT wrap the entire flow in a single
    transaction. v4 did, and Codex flagged that if process.start()
    succeeded but the post-spawn flush/commit failed, the transaction
    would roll back, leaving a live trading subprocess with no committed
    row. The next retry would then launch a duplicate.

    v5 splits spawn into three phases, each in its own transaction:

    Phase A — Reserve the slot (one transaction):
        - SELECT FOR UPDATE the live_deployments row
        - Look up any existing active live_node_processes row
          (active = starting, building, ready, running, stopping)
        - If active exists → return True (idempotent success)
        - INSERT new row with status='starting', pid=NULL
        - COMMIT (this is what claims the partial unique index slot)

    Phase B — Spawn outside any transaction:
        - Re-check the halt flag (decision #16, Codex v4 P0):
          if msai:risk:halt is set, UPDATE the row to status='failed'
          with reason='blocked by halt flag' and return True
          (the row is gone, the next /start will succeed after /resume)
        - mp.Process(...).start() — irreversible side effect, NO DB
          transaction wrapping it
        - Stash the handle in self._handles

    Phase C — Record the pid (one transaction):
        - UPDATE live_node_processes.pid = process.pid
        - COMMIT

    If anything fails between phase A and phase C, the row sits in
    status='starting' with pid=NULL. The HeartbeatMonitor (which now
    includes 'building' AND 'starting' in its stale sweep) will time
    it out within stale_seconds and flip it to 'failed'. The next
    retry then succeeds because the unique index slot is free.

    Handle map (decision #15):
    - self._handles maps deployment_id → mp.Process while the supervisor
      is alive. Used by reap_loop for instant exit detection (parent and
      child are in the same Linux namespace, so Process.is_alive() works).
    - On supervisor restart, the map is empty. Rediscovery is via the
      heartbeat: stale rows are flipped to 'failed' by HeartbeatMonitor;
      fresh rows are still running.
    """

    def __init__(self, db: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self._db = db
        self._redis = redis
        self._handles: dict[UUID, mp.Process] = {}

    async def spawn(
        self,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict,
        idempotency_key: str,
    ) -> bool:
        """Spawn a new trading subprocess. Returns True on success or
        idempotent no-op, False on hard failure (caller should NOT ACK).
        """
        # ─── Phase A: reserve the slot ────────────────────────────────
        row_id: UUID | None = None
        async with self._db() as session, session.begin():
            deployment = (await session.execute(
                select(LiveDeployment)
                .where(LiveDeployment.deployment_slug == deployment_slug)
                .with_for_update()
            )).scalar_one_or_none()
            if deployment is None:
                logger.error("spawn_no_deployment", deployment_slug=deployment_slug)
                return False  # Hard failure → caller does not ACK

            existing = (await session.execute(
                select(LiveNodeProcess).where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    # NOTE: 'stopping' included — a start during a stop is NOT
                    # idempotent success and must NOT race the in-flight stop
                    # (Codex v4 P0). The caller's command stays in the PEL for
                    # XAUTOCLAIM retry after the stopping row reaches a
                    # terminal state.
                    LiveNodeProcess.status.in_(
                        ("starting", "building", "ready", "running", "stopping")
                    ),
                )
            )).scalar_one_or_none()
            if existing is not None:
                if existing.status == "stopping":
                    logger.info(
                        "spawn_during_stop_busy",
                        deployment_id=str(deployment_id),
                    )
                    return False  # Caller does not ACK; retry after stop completes
                logger.info("spawn_idempotent", deployment_id=str(deployment_id))
                return True  # Already active → idempotent success

            try:
                row = LiveNodeProcess(
                    deployment_id=deployment_id,
                    pid=None,  # filled in phase C
                    host=socket.gethostname(),
                    started_at=utcnow(),
                    last_heartbeat_at=utcnow(),
                    status="starting",
                )
                session.add(row)
                await session.flush()  # validates partial unique index
                row_id = row.id
            except IntegrityError:
                # Partial unique index caught a race — another supervisor
                # instance won. Treat as idempotent success.
                logger.info("spawn_race_idempotent", deployment_id=str(deployment_id))
                return True
            # COMMIT happens at session.begin() exit ↑

        # ─── Phase B: halt-flag check + spawn (NO db transaction) ──────
        # Decision #16, Codex v4 P0: re-check the halt flag BEFORE spawning.
        # The HTTP endpoint also checks it, but a command queued before
        # /kill-all (or reclaimed from the PEL after) could still reach
        # the supervisor. This is the supervisor-side enforcement.
        if await self._redis.exists("msai:risk:halt"):
            logger.warning("spawn_blocked_by_halt", deployment_id=str(deployment_id))
            await self._mark_failed(
                row_id,
                reason="blocked by halt flag",
                failure_kind=FailureKind.HALT_ACTIVE,  # v8 (Codex v7 P1)
            )
            return True  # Successfully handled — caller ACKs (no retry until /resume)

        try:
            ctx = mp.get_context("spawn")
            process = ctx.Process(
                target=_trading_node_subprocess,
                args=(TradingNodePayload.from_dict(payload),),
            )
            process.start()
        except Exception as exc:
            logger.exception("spawn_process_start_failed", deployment_id=str(deployment_id))
            await self._mark_failed(
                row_id,
                reason=f"process.start() failed: {exc}",
                failure_kind=FailureKind.SPAWN_FAILED_PERMANENT,  # v8 (Codex v7 P1)
            )
            return True  # Caller ACKs; the row is in 'failed' so the next retry will succeed

        self._handles[deployment_id] = process

        # ─── Phase C: record the pid ──────────────────────────────────
        try:
            async with self._db() as session, session.begin():
                row = await session.get(LiveNodeProcess, row_id, with_for_update=True)
                if row is not None:
                    row.pid = process.pid
        except Exception:
            # If phase C fails, the heartbeat the subprocess writes will
            # advance last_heartbeat_at without us ever recording the pid.
            # That's fine for liveness (heartbeat is the authority), but
            # the stop() path falls back to reading pid from the row, so
            # we MUST record it. If we can't, log loudly and let the next
            # operator action (kill-all → SIGTERM via handle map) handle it.
            logger.exception("spawn_pid_update_failed", deployment_id=str(deployment_id), pid=process.pid)
            # The handle map still has the live process, so reap_loop
            # and stop() (via self._handles[deployment_id]) still work.

        return True

    async def _mark_failed(
        self,
        row_id: UUID | None,
        reason: str,
        failure_kind: FailureKind,  # v8: REQUIRED (Codex v7 P1)
    ) -> None:
        """Mark a row as failed with a structured failure_kind.

        v8 change: failure_kind is now a REQUIRED parameter. v7 added
        the column to the schema but the writer never populated it,
        leaving /start unable to classify failures.
        """
        if row_id is None:
            return
        async with self._db() as session, session.begin():
            row = await session.get(LiveNodeProcess, row_id)
            if row is None:
                return
            row.status = "failed"
            row.failure_kind = failure_kind.value
            row.error_message = reason
            row.exit_code = None

    async def stop(self, deployment_id: UUID, reason: str = "user") -> bool:
        """Send SIGTERM to the subprocess. Escalate to SIGKILL after 30s.

        Returns True on success (or idempotent no-op), False on hard failure.

        Uses self._handles[deployment_id] for in-process cases. For supervisor-
        restart-discovered subprocesses (no handle), reads pid from the row
        and signals it directly. Both paths are in the same container
        namespace so the pid is meaningful.
        """
        async with self._db() as session:
            row = (await session.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment_id,
                    LiveNodeProcess.status.in_(
                        ("starting", "building", "ready", "running")
                    ),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if row is None:
                logger.info("stop_idempotent", deployment_id=str(deployment_id))
                return True  # Already stopped
            row.status = "stopping"
            await session.commit()

        process = self._handles.get(deployment_id)
        pid = process.pid if process is not None else row.pid
        if pid is None:
            # Phase-C failure path: row exists, status='stopping', no pid.
            # Heartbeat monitor will time it out. Treat the stop as
            # successful from the caller's POV.
            logger.warning("stop_no_pid", deployment_id=str(deployment_id))
            return True

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True  # Already gone — reap_loop catches up

        # Wait up to 30s for the row to flip to stopped/failed
        async with self._db() as session:
            for _ in range(30):
                await asyncio.sleep(1)
                cur = await session.get(LiveNodeProcess, row.id)
                if cur.status in ("stopped", "failed"):
                    return True
            # Escalate
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            cur.status = "failed"
            cur.error_message = "hard kill on stop timeout"
            cur.exit_code = -9
            await session.commit()
        return True

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        """Background task: poll handles for exits, surface real exit codes.

        Decision #15: parent + child are in the same namespace, so
        Process.is_alive() and Process.exitcode are meaningful and
        give instant exit detection. Heartbeat is only the recovery
        signal across supervisor restarts.
        """
        while not stop_event.is_set():
            for deployment_id, proc in list(self._handles.items()):
                if not proc.is_alive():
                    proc.join(timeout=1)
                    await self._on_child_exit(deployment_id, proc.exitcode)
                    del self._handles[deployment_id]
            await asyncio.sleep(1)

    async def watchdog_loop(
        self,
        stop_event: asyncio.Event,
        default_stale_seconds: int = 30,
        default_startup_hard_timeout_s: int = 1800,  # v8: was 600 (Codex v7 P2)
    ) -> None:
        """Background task: SOLE liveness authority for rows in
        status IN ('starting','building'). Uses the lock-first
        atomic path so the SIGKILL and the UPDATE are guaranteed
        consistent.

        v8 changes from v7 (Codex v7 P0 + P2):

        1. **Lock-first atomic path.** v7 was `scan → SIGKILL →
           SELECT FOR UPDATE → maybe UPDATE`, which had a race: if
           the child flipped to `ready` or `stopping` between the
           initial scan and the kill, the post-kill SELECT FOR
           UPDATE's `status IN ('starting','building')` filter would
           miss the row, the UPDATE would be skipped, and the dead
           process would leave a row in `ready`/`stopping` with
           no writer. v8 holds a row-level lock across the entire
           kill-and-update sequence:

               BEGIN;
               SELECT ... FOR UPDATE
                 WHERE id = :row_id
                   AND status IN ('starting','building');
               -- still in scope under the lock?
               if row is None → COMMIT (noop, benign race)
               os.kill(pid, SIGKILL)
               UPDATE status='failed', failure_kind=..., exit_code=-9
               COMMIT;  -- releases the lock

           Postgres row-level lock blocks any concurrent writer
           (heartbeat thread UPDATE, /stop handler, reap_loop) from
           flipping the status while the kill is in flight. After
           the COMMIT, the row is in its final 'failed' state
           atomically with the kill.

        2. **Default hard timeout raised to 1800s** (Codex v7 P2).
           docs/nautilus-reference.md:482,513 documents that large
           options universes can legitimately take 900s+ to build.
           v7's 600s default would false-kill them. 1800s covers
           realistic worst cases. Operators with larger setups can
           override per-deployment via live_deployments.startup_hard_timeout_s.

        3. **Per-deployment override.** The watchdog reads
           live_deployments.startup_hard_timeout_s (nullable) for
           each row; NULL falls back to default_startup_hard_timeout_s.

        Still in effect from v7:
        - Heartbeat-based primary kill condition
        - Watchdog is SOLE liveness authority for startup statuses
          (HeartbeatMonitor excludes them — see decision #17)
        - All reasons for the kill are recorded in failure_kind +
          error_message (v8: failure_kind is REQUIRED on writes — see
          _mark_failed in ProcessManager)
        """
        while not stop_event.is_set():
            # Scan candidates (non-locking read — just to know which
            # rows to examine). The actual kill decision is made inside
            # the per-row locked transaction.
            async with self._db() as session:
                now = utcnow()
                # Candidate rows: startup status + (stale heartbeat OR hard ceiling past)
                candidate_rows = (await session.execute(
                    select(LiveNodeProcess.id, LiveNodeProcess.deployment_id)
                    .join(LiveDeployment, LiveDeployment.id == LiveNodeProcess.deployment_id)
                    .where(
                        LiveNodeProcess.status.in_(("starting", "building")),
                        or_(
                            LiveNodeProcess.last_heartbeat_at < now - timedelta(seconds=default_stale_seconds),
                            LiveNodeProcess.started_at < now - timedelta(
                                seconds=func.coalesce(
                                    LiveDeployment.startup_hard_timeout_s,
                                    default_startup_hard_timeout_s,
                                )
                            ),
                        ),
                    )
                )).all()

            for row_id, deployment_id in candidate_rows:
                # Per-row locked transaction wrapped in asyncio.wait_for
                # as a safety belt (v9, Codex v8 P1): a Postgres-side
                # lock contention bounded by a 5s outer timeout means
                # the candidate loop can make forward progress even
                # when one row is contended.
                try:
                    await asyncio.wait_for(
                        self._watchdog_kill_one(
                            row_id=row_id,
                            deployment_id=deployment_id,
                            stale_seconds=default_stale_seconds,
                            default_hard_timeout_s=default_startup_hard_timeout_s,
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "watchdog_lock_timeout",
                        row_id=str(row_id),
                        deployment_id=str(deployment_id),
                        note="row locked by concurrent writer; retry next iteration",
                    )
                except Exception as exc:
                    logger.exception(
                        "watchdog_kill_failed",
                        row_id=str(row_id),
                        deployment_id=str(deployment_id),
                        error=str(exc),
                    )

            await asyncio.sleep(5)

    async def _watchdog_kill_one(
        self,
        row_id: UUID,
        deployment_id: UUID,
        stale_seconds: int,
        default_hard_timeout_s: int,
    ) -> None:
        """Lock-first atomic kill. Called from watchdog_loop per
        candidate row, wrapped in asyncio.wait_for(timeout=5).

        Inside a single transaction:
        1. SELECT FOR UPDATE SKIP LOCKED the row (v9 — Codex v8 P1:
           skip-locked means a contended row is silently no-oped
           instead of blocking the whole candidate loop)
        2. Re-check: is it still in a startup status? Is it still
           stale (heartbeat OR hard ceiling)? If not, COMMIT and
           return — benign race, row moved out of scope or caught up.
        3. Determine pid from row.pid OR self._handles[deployment_id].pid
           (v9 — Codex v8 P0: fallback to handle map when phase-C
           never persisted the pid). If neither source has a pid,
           log ERROR, do NOT flip the row to failed, return — next
           iteration will try again.
        4. os.kill(pid, SIGKILL) — synchronous, in-kernel
        5. UPDATE row: status='failed', failure_kind=BUILD_TIMEOUT,
           exit_code=-9, error_message=reason
        6. COMMIT — releases the lock

        No concurrent writer can interleave between steps 2 and 5
        because the row-level lock is held for the whole transaction.
        """
        async with self._db() as session, session.begin():
            now = utcnow()
            # 1. Acquire the lock + read the row. SKIP LOCKED returns
            # nothing if the row is held by another writer — the
            # candidate gets retried on the next iteration (5s later).
            result = await session.execute(
                select(LiveNodeProcess, LiveDeployment.startup_hard_timeout_s)
                .join(LiveDeployment, LiveDeployment.id == LiveNodeProcess.deployment_id)
                .where(LiveNodeProcess.id == row_id)
                .with_for_update(of=LiveNodeProcess, skip_locked=True)
            )
            record = result.one_or_none()
            if record is None:
                # Either the row was deleted or it's currently locked
                # by another transaction. Either way: skip this pass.
                return
            row, per_deployment_hard_timeout = record

            # 2. Re-check status UNDER THE LOCK. If the subprocess
            # flipped to ready/stopping/stopped/failed between the
            # scan and the lock, the benign race wins.
            if row.status not in ("starting", "building"):
                logger.info(
                    "watchdog_race_skipped",
                    row_id=str(row.id),
                    current_status=row.status,
                )
                return

            # Re-check the kill conditions under the lock.
            effective_hard_timeout = per_deployment_hard_timeout or default_hard_timeout_s
            heartbeat_stale = row.last_heartbeat_at < now - timedelta(seconds=stale_seconds)
            hard_ceiling_hit = row.started_at < now - timedelta(seconds=effective_hard_timeout)

            if not (heartbeat_stale or hard_ceiling_hit):
                logger.info(
                    "watchdog_progress_detected",
                    row_id=str(row.id),
                    last_heartbeat_at=row.last_heartbeat_at.isoformat(),
                )
                return

            reason = (
                f"watchdog: no heartbeat progress for > {stale_seconds}s"
                if heartbeat_stale
                else f"watchdog: hard wall-clock timeout > {effective_hard_timeout}s"
            )

            # 3. Determine pid — row.pid with self._handles fallback
            # (v9, Codex v8 P0). Phase-C can leave row.pid=NULL while
            # the live mp.Process is still in self._handles; without
            # this fallback the watchdog would flip the row to failed
            # without killing the child, which could survive and cause
            # a retry duplicate spawn.
            pid_to_kill = row.pid
            handle = self._handles.get(deployment_id)
            if pid_to_kill is None and handle is not None:
                pid_to_kill = handle.pid

            if pid_to_kill is None:
                # Neither row.pid nor handle.pid is populated. This is
                # an unexpected state — log, alert, and DO NOT flip the
                # row. Next iteration (or the subprocess's own finally)
                # will converge. v9 (Codex v8 P0): do NOT make the row
                # terminal without a kill, because the live child could
                # still be running invisibly.
                logger.error(
                    "watchdog_no_pid_giveup_this_pass",
                    row_id=str(row.id),
                    deployment_id=str(row.deployment_id),
                    reason=reason,
                )
                alert_service.fire("watchdog_no_pid", deployment_id=str(deployment_id))
                return

            logger.error(
                "watchdog_kill",
                row_id=str(row.id),
                deployment_id=str(row.deployment_id),
                pid=pid_to_kill,
                pid_source="row" if row.pid is not None else "handle_map",
                status=row.status,
                started_at=row.started_at.isoformat(),
                last_heartbeat_at=row.last_heartbeat_at.isoformat(),
                reason=reason,
            )

            # 4. SIGKILL — synchronous, completes before the next line
            # runs. The subprocess cannot write to the row after this
            # because the row lock is still held AND the process is dead.
            try:
                os.kill(pid_to_kill, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone — we still own the row update

            # 5. UPDATE the row in the same transaction
            row.status = "failed"
            row.failure_kind = FailureKind.BUILD_TIMEOUT.value
            row.error_message = reason
            row.exit_code = -9
            # COMMIT happens at session.begin() exit — releases the lock

        # Outside the transaction: drop the handle and fire the alert.
        self._handles.pop(deployment_id, None)
        alert_service.fire("watchdog_kill", deployment_id=str(deployment_id))

    async def _on_child_exit(self, deployment_id: UUID, exit_code: int | None) -> None:
        """Called when self._handles[deployment_id] is no longer alive.

        Updates live_node_processes.status to 'stopped' (exit_code 0)
        or 'failed' (non-zero), records the real exit_code, and emits
        an alert if the exit was unexpected.

        v9 (Codex v8 P2): writes failure_kind. exit_code == 0 maps to
        FailureKind.NONE (clean exit), non-zero maps to
        FailureKind.SPAWN_FAILED_PERMANENT (the subprocess died
        without writing a more specific failure_kind — the only
        finer-grained writer is the subprocess's own finally block,
        which already sets failure_kind before exiting, so a reap-loop
        observation with a still-NULL failure_kind means the subprocess
        never got to the finally or died uncleanly).
        """
        async with self._db() as session, session.begin():
            row = (
                await session.execute(
                    select(LiveNodeProcess)
                    .where(
                        LiveNodeProcess.deployment_id == deployment_id,
                        LiveNodeProcess.status.in_(
                            ("starting", "building", "ready", "running", "stopping")
                        ),
                    )
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            if exit_code == 0:
                row.status = "stopped"
                # Only overwrite failure_kind if it's still NULL —
                # don't clobber a more specific value the subprocess
                # already wrote in its finally block.
                if row.failure_kind is None:
                    row.failure_kind = FailureKind.NONE.value
            else:
                row.status = "failed"
                row.error_message = f"child exited with code {exit_code}"
                if row.failure_kind is None:
                    row.failure_kind = FailureKind.SPAWN_FAILED_PERMANENT.value
            row.exit_code = exit_code
```

**Watchdog + HeartbeatMonitor wiring at supervisor startup (`live_supervisor/main.py`):**

```python
async def run_forever() -> None:
    ...
    stop_event = asyncio.Event()
    # HeartbeatMonitor owns post-startup rows (ready/running/stopping)
    monitor_task = asyncio.create_task(heartbeat_monitor.run_forever(stop_event))
    # Watchdog owns startup rows (starting/building) — SOLE liveness authority there
    watchdog_task = asyncio.create_task(
        process_manager.watchdog_loop(
            stop_event,
            default_stale_seconds=settings.startup_stale_seconds,  # default 30
            # v8: default raised from 600s to 1800s to cover large options universes.
            # Per-deployment overrides live on live_deployments.startup_hard_timeout_s.
            default_startup_hard_timeout_s=settings.startup_hard_timeout_s,  # default 1800
        )
    )
    reap_task = asyncio.create_task(process_manager.reap_loop(stop_event))
    ...
    try:
        async for command in bus.consume(...):
            ...
    finally:
        monitor_task.cancel()
        reap_task.cancel()
        watchdog_task.cancel()
```

`heartbeat_monitor.py`:

```python
class HeartbeatMonitor:
    """Post-startup orphan detector. Cross-restart recovery for
    deployments that were running but lost their parent supervisor.

    v7 change (Codex v6 P0): the stale-sweep query EXCLUDES
    'starting' and 'building'. The watchdog (ProcessManager.watchdog_loop)
    has sole authority over startup liveness — it kills the pid BEFORE
    flipping the row to failed, so there's no window where a startup
    row is out of the active set but the process is still alive.

    v6 included 'starting'+'building' in the sweep, which raced the
    watchdog's wall-clock deadline and allowed retries to spawn
    duplicate children. v7 removes the overlap entirely.

    Stale-sweep query:
        SELECT * FROM live_node_processes
        WHERE status IN ('ready','running','stopping')
          AND last_heartbeat_at < now() - interval ':stale_seconds seconds'

    Why 'stopping' is INCLUDED: a stop command that never completes
    (supervisor crashed mid-stop) leaves the row in 'stopping'. If
    the subprocess later dies without the supervisor observing the
    exit, the stale sweep catches it. Alternatively, the supervisor
    reap_loop catches it on the next restart via the heartbeat freshness
    check in the recovery discovery path (4.4).
    """
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._mark_stale_as_failed()
            await asyncio.sleep(10)

    async def _mark_stale_as_failed(self) -> None:
        async with self._db() as session, session.begin():
            stale = await session.execute(
                update(LiveNodeProcess)
                .where(
                    # v7: startup statuses EXCLUDED — watchdog owns them
                    LiveNodeProcess.status.in_(("ready", "running", "stopping")),
                    LiveNodeProcess.last_heartbeat_at
                    < utcnow() - timedelta(seconds=self._stale_seconds),
                )
                .values(
                    status="failed",
                    error_message="heartbeat timeout",
                    # v9 (Codex v8 P2): write failure_kind. Post-startup
                    # stale means the subprocess died without reporting
                    # a more specific failure — UNKNOWN is the honest
                    # classification. The endpoint only reads failure_kind
                    # for pre-ready outcomes, so this value is primarily
                    # for operator visibility.
                    failure_kind=FailureKind.UNKNOWN.value,
                )
                .returning(LiveNodeProcess.deployment_id)
            )
            for (deployment_id,) in stale.fetchall():
                logger.error(
                    "heartbeat_stale_marked_failed",
                    deployment_id=str(deployment_id),
                    stale_seconds=self._stale_seconds,
                )
                alert_service.fire("heartbeat_stale", deployment_id=str(deployment_id))
```

The docker-compose service:

```yaml
live-supervisor:
  build:
    context: ./backend
    dockerfile: Dockerfile.dev
  container_name: msai-claude-live-supervisor
  command: ["python", "-m", "msai.live_supervisor"]
  volumes:
    - ./backend/src:/app/src:ro
    - ./strategies:/app/strategies:ro
    - ./data:/app/data
  environment:
    DATABASE_URL: postgresql+asyncpg://msai:msai_dev_password@postgres:5432/msai
    REDIS_URL: redis://redis:6379
    MSAI_API_KEY: ${MSAI_API_KEY:-msai-dev-key}
  depends_on:
    postgres: { condition: service_healthy }
    redis: { condition: service_healthy }
    ib-gateway: { condition: service_started }
  restart: unless-stopped
```

TDD:

1. Unit test `ProcessManager.spawn` with a patched `multiprocessing`: verify a row is inserted with `status="starting"` and `pid=None`, then updated with the real pid after `start()` (phase-C). Note: the subprocess also self-writes its pid in 1.8; this tests the supervisor fallback path.
2. **Idempotency unit test #1**: pre-seed an active row for `deployment_id`, call `spawn(deployment_id)`, verify it returns True without spawning a new process and without inserting a new row
3. **Idempotency unit test #2**: simulate a race — patch `INSERT` to raise `IntegrityError` (the partial unique index fired), assert `spawn` catches it and returns True
4. **Start-during-stop test (Codex v4 P0)**: pre-seed an active row with `status='stopping'`, call `spawn`, assert it returns **False** (busy), not True — the command stays in the PEL for XAUTOCLAIM retry after the stopping row terminates
5. Unit test `ProcessManager.stop` with the handle map populated: verify SIGTERM, wait loop, SIGKILL escalation, real exit code recorded
6. Unit test `ProcessManager.stop` when the handle map is empty (rediscovered subprocess after a supervisor restart): verify pid is read from the row (populated by the subprocess self-write in 1.8) and signaled successfully
7. **Stop-after-supervisor-restart test (Codex v5 P0 regression)**: seed a row with `status='running'`, `pid=<live pid>`, clear the handle map (simulating a supervisor restart), call `stop(deployment_id)` — verify `os.kill(pid, SIGTERM)` IS called (NOT a silent success)
8. **Reap loop unit test**: stash a fake `Process` whose `is_alive()` returns False and `exitcode == 1`, run one iteration of `reap_loop`, verify `live_node_processes` row is `status='failed'`, `exit_code=1`, error_message contains "child exited with code 1"
9. **Watchdog unit test — no-progress kill (v7, Codex v6 P1 regression)**: seed a row with `status='building'`, `pid=<live pid of a fake sleeping child>`, `last_heartbeat_at = now() - 45s` (past the 30s stale threshold), `started_at = now() - 50s` (well within the 1800s hard backstop), run one iteration of `watchdog_loop`, verify:
   - `os.kill(pid, SIGKILL)` IS called
   - The row flips to `status='failed'`, `error_message` contains "no heartbeat progress", `exit_code=-9`
   - The handle is removed from `self._handles`
   - The alert service fires
10. **Watchdog unit test — slow-but-healthy build untouched (v7, Codex v6 P1 regression)**: seed a row with `status='building'`, `started_at = now() - 300s` (well past v6's old 180s wall-clock), but `last_heartbeat_at = now() - 5s` (heartbeat thread still advancing). Run `watchdog_loop`. Verify the row is NOT killed, NOT marked failed, and `os.kill` is NOT called. This is the regression test for the slow-IB-contract-loading case.
11. **Watchdog unit test — hard wall-clock backstop**: seed a row with `status='building'`, `last_heartbeat_at = now() - 10s` (not stale enough to trip the primary condition), BUT `started_at = now() - 2000s` (past the 1800s hard backstop). Verify the row IS killed via the secondary backstop path (`error_message` contains "hard wall-clock timeout").
12. **Watchdog per-deployment override test (v8)**: seed a row with `started_at = now() - 1000s` (past default 1800s? no, within), set `live_deployments.startup_hard_timeout_s = 500` for that deployment, run watchdog — verify the row IS killed via the per-deployment override (500s exceeded, 1000s > 500s).
13. **Watchdog pid-fallback test (v9, Codex v8 P0 regression)**: seed a row with `pid=NULL` (simulating phase-C failure) but stash a live `mp.Process` in `self._handles[deployment_id]` (handle.pid is a real pid). Make heartbeat stale. Run watchdog — verify `os.kill(handle.pid, SIGKILL)` IS called (pid sourced from the handle map), row flips to `failed`, handle is dropped. The pid_source log field should be "handle_map".
14. **Watchdog no-pid giveup test (v9, Codex v8 P0 regression)**: seed a row with `pid=NULL` AND an empty `self._handles` (supervisor restart scenario — the handle map was wiped). Run watchdog — verify `os.kill` is NOT called, the row is NOT flipped to `failed` (stays in `building`), an alert fires with name `watchdog_no_pid`. The next iteration (or the subprocess's own convergence) will handle it.
15. **Watchdog SKIP LOCKED test (v9, Codex v8 P1 regression)**: open a concurrent transaction that holds a row-level lock on one candidate row. Run the watchdog loop. Verify the locked row is silently skipped (no exception) and that OTHER candidate rows in the same pass ARE still processed. Also verify that the `asyncio.wait_for(timeout=5)` outer safety belt bounds the per-row time so even a hung DB doesn't block the whole loop beyond 5s per row.
16. **Watchdog ownership test (v7/v9, Codex v6 P0 regression)**: assert that running both the HeartbeatMonitor AND the Watchdog concurrently against a stale-heartbeat `building` row produces exactly ONE kill (by the Watchdog) and the HeartbeatMonitor DOES NOT touch the row at all. Verify by checking `os.kill` was called AND the row's final `error_message` is the watchdog message ("no heartbeat progress" or "hard wall-clock timeout"), not "heartbeat timeout".
17. **HeartbeatMonitor excludes startup statuses test (v9, Codex v8 P1 regression)**: seed `status='starting'`, `status='building'` rows with stale heartbeats. Run `HeartbeatMonitor._mark_stale_as_failed`. Verify NEITHER row is touched (the query filters them out).
18. **Watchdog untouched by ready/running/stopping**: seed rows with `status='ready'`, `status='running'`, `status='stopping'`, all with stale heartbeats. Run `watchdog_loop`. Verify NONE are touched — those statuses belong to the HeartbeatMonitor.
19. **HeartbeatMonitor ownership test (v7, Codex v6 P0 regression)**: seed rows with `status='starting'` and `status='building'` with stale heartbeats. Run `HeartbeatMonitor._mark_stale_as_failed`. Verify NEITHER is touched — startup statuses belong to the watchdog.
20. **HeartbeatMonitor stale sweep**: seed rows with `status='ready'`, `status='running'`, `status='stopping'` with stale heartbeats. Run `HeartbeatMonitor._mark_stale_as_failed`. Verify all three flip to `failed`.
21. **ACK-on-success-only test**: invoke `run_forever`'s command handler with a mock that returns False (failure), assert `bus.ack` is NOT called; with True (success), assert ack IS called
22. Integration test against testcontainers Postgres + Redis: publish a start command via `LiveCommandBus`, verify the supervisor consumes it, inserts a row, calls `_trading_node_subprocess` (use a no-op stub)
23. Integration test: publish two `start` commands for the same deployment_id back-to-back, verify only one trading subprocess is spawned and the second command is also ACKed
24. Implement

Acceptance: tests pass; the service stands up in `docker compose up -d live-supervisor`.

Effort: L
Depends on: 1.1, 1.1b, 1.5, 1.6
Gotchas: Codex v3 P0 (idempotency at DB + supervisor + ACK-on-success), Codex v3 P2 / v5 P0 (handle map for instant exit detection; subprocess self-writes pid), Codex v6 P0 (single startup-liveness authority — watchdog owns startup, heartbeat monitor owns post-startup; no overlap), Codex v6 P1 (heartbeat-based watchdog deadline, not wall-clock), #18 (asyncio.run conflict)

---

#### 1.8 — Trading subprocess entry point (self-writes pid; canonical health check; watchdog is supervisor-side)

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (full rewrite)
- `claude-version/backend/src/msai/services/nautilus/startup_health.py` (new)
- `claude-version/backend/tests/unit/test_trading_node_subprocess.py` (new)
- `claude-version/backend/tests/unit/test_startup_health.py` (new)

Top-level function (must be importable for `spawn` pickling):

```python
def _trading_node_subprocess(payload: TradingNodePayload) -> None:
    """Entry point for the live trading subprocess.

    Runs in a fresh Python interpreter under the spawn context.

    Two v6 changes from v5:

    1. The subprocess SELF-WRITES its pid immediately after connecting
       to Postgres (before anything else). This guarantees
       live_node_processes.pid is populated on every code path, so
       /stop and /kill-all work via pid even if the supervisor's
       in-memory handle map is empty after a supervisor restart
       (Codex v5 P0 fix). Phase-C UPDATE in ProcessManager.spawn is
       now a belt-and-suspenders backup, not the primary pid source.

    2. node.build() is NOT wrapped in asyncio.wait_for. v5 tried that
       and Codex v5 P0 pointed out that asyncio.wait_for only cancels
       the awaiter, not the executor thread — a wedged C-side IB build
       can't be stopped from inside the subprocess. v6 lets node.build()
       run normally. Wedged builds are killed from OUTSIDE: the
       supervisor's ProcessManager.watchdog_loop (1.7) SIGKILLs any
       child that hasn't reached status='ready' or 'failed' within
       build_timeout_s + startup_health_timeout_s (default 180s total).

    Steps:

    1. Import nautilus_trader (installs uvloop policy globally — gotcha #1)
    2. Reset asyncio event loop policy to default (gotcha #18)
    3. Connect to Postgres
    4. **SELF-WRITE PID**: UPDATE live_node_processes SET pid=os.getpid(),
       status='building', last_heartbeat_at=now() WHERE id=row_id
       (Codex v5 P0 fix — guarantees pid is populated)
    5. Start the heartbeat thread (continues the heartbeat the subprocess
       wrote in step 4). Heartbeat starts BEFORE node.build() so a hung
       build ages out via the HeartbeatMonitor stale sweep (decision #17).
    6. Install the SIGTERM handler
    7. Build the TradingNodeConfig via build_live_trading_node_config
       - trader_id = f"MSAI-{deployment_slug}" (decision #7, stable)
       - strategies[0].order_id_tag = deployment_slug
       - manage_stop = True (native flatten on stop)
    8. Construct TradingNode and register IB factories under key "IB"
    9. node.build() — synchronous. v6 does NOT wrap in asyncio.wait_for.
       A wedged build is killed from outside by the supervisor watchdog
       (1.7). Normal builds complete in seconds.
    10. await node.start_async() — kernel internally awaits engine
        connect → reconciliation → portfolio init → trader.start.
        Each await silently early-returns on failure. Verified at
        nautilus_trader/system/kernel.py:1022-1037.
    11. POST-START HEALTH CHECK (decision #14): await wait_until_ready(node)
        - Polls node.kernel.trader.is_running until True or timeout
        - Canonical FSM signal — only trips after _trader.start() runs
          on the LAST line of start_async (verified kernel.py:1037)
        - On timeout: raises StartupHealthCheckFailed with diagnose(node)
    12. Update LiveNodeProcess.status='ready'
    13. node.run() — blocks until SIGTERM
    14. finally:
        - Heartbeat thread stopped
        - node.stop_async() — Nautilus closes positions / cancels orders
          via manage_stop=True
        - node.dispose() — releases Rust logger and sockets (gotcha #20)
        - On clean exit: status='stopped', failure_kind=NONE, exit_code=0
        - On StartupHealthCheckFailed (v8, Codex v7 P1):
          status='failed', failure_kind=RECONCILIATION_FAILED,
          error_message=diagnosis, exit_code=2
          (RECONCILIATION_FAILED is the closest match — the
          subprocess can't distinguish engine-connect failure from
          reconciliation failure from portfolio init failure without
          reading internal state; the diagnosis string in
          error_message has the details)
        - On any other exception during build/start: status='failed',
          failure_kind=SPAWN_FAILED_PERMANENT, error_message=traceback,
          exit_code=1
        - On any other exception during node.run() (post-ready):
          status='failed', failure_kind=SPAWN_FAILED_PERMANENT,
          error_message=traceback, exit_code=1
          (post-ready exceptions are still permanent for idempotency
          purposes — the endpoint has already returned; the cached
          201 is correct for that deployment, and the next attempt
          at the same identity_signature will go through the endpoint
          again because the caller's retry has a different logical
          lifecycle)
"""
```

`startup_health.py` — verified against `nautilus_trader 1.223.0`:

```python
import asyncio
from time import monotonic


class StartupHealthCheckFailed(Exception):
    """Raised when the post-start health check times out.

    The message is the structured diagnosis from diagnose() listing
    the values of every relevant Nautilus accessor at timeout, so
    log triage can pinpoint which step failed (engine connect,
    reconciliation, portfolio init, instrument loading).
    """


async def wait_until_ready(node: "TradingNode", timeout_s: int = 60) -> None:
    """After node.start_async() returns, verify the trader actually started.

    Canonical signal: node.kernel.trader.is_running (a property on
    Trader/Component, at common/component.pyx:1768-1779). The trader
    FSM transitions to RUNNING only inside the LAST line of
    kernel.start_async() (self._trader.start() at kernel.py:1037),
    which is reached only on full success of every internal await.

    A brief poll handles the rare async-task scheduling window where
    _trader.start() has been queued but the FSM hasn't flipped yet.
    """
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        if node.kernel.trader.is_running:
            return
        await asyncio.sleep(0.5)
    raise StartupHealthCheckFailed(diagnose(node))


def diagnose(node: "TradingNode") -> str:
    """Structured failure-reason string using the REAL Nautilus
    accessors (verified against 1.223.0):

    - kernel.trader.is_running        — property on Trader/Component
    - data_engine.check_connected()   — METHOD (data/engine.pyx:296)
    - exec_engine.check_connected()   — METHOD (execution/engine.pyx:269)
    - <client>.reconciliation_active  — per-LiveExecutionClient flag
                                        (live/execution_client.py:136)
    - portfolio.initialized           — attribute (portfolio.pyx:218)
    - len(cache.instruments())        — current instrument count

    NOTE on exec_engine._clients: registered_clients is a list[ClientId]
    (execution/engine.pyx:204-214). The dict of ExecutionClient objects
    is the private _clients attribute. We access it directly here
    because diagnose() runs inside the subprocess that constructed the
    kernel — same process, no abstraction boundary crossed.
    """
    kernel = node.kernel
    parts = [f"trader.is_running={kernel.trader.is_running}"]

    try:
        parts.append(f"data_engine.check_connected()={kernel.data_engine.check_connected()}")
    except Exception as e:
        parts.append(f"data_engine.check_connected()=<error: {e}>")

    try:
        parts.append(f"exec_engine.check_connected()={kernel.exec_engine.check_connected()}")
    except Exception as e:
        parts.append(f"exec_engine.check_connected()=<error: {e}>")

    # Per-client reconciliation_active via the private _clients dict.
    # Acceptable because we're in-process (same Python interpreter that
    # built the kernel). Public registered_clients returns list[ClientId],
    # not client objects.
    try:
        clients_dict = getattr(kernel.exec_engine, "_clients", {})
        for client_id, client in clients_dict.items():
            recon = getattr(client, "reconciliation_active", None)
            connected = getattr(client, "is_connected", None)
            parts.append(f"{client_id}.reconciliation_active={recon},is_connected={connected}")
    except Exception as e:
        parts.append(f"exec_engine._clients=<error: {e}>")

    parts.append(f"portfolio.initialized={getattr(kernel.portfolio, 'initialized', None)}")
    parts.append(f"cache.instruments_count={len(kernel.cache.instruments())}")

    return "; ".join(parts)
```

The deterministic identities from decision #7 are injected here. `payload.deployment_slug` comes from the supervisor, which reads it from the `live_deployments` row (warm restart) or generates a fresh one (cold start).

TDD:

1. Unit test `wait_until_ready` with a mock node where `kernel.trader.is_running` flips to True on the third poll — verify it returns
2. Unit test `wait_until_ready` with a mock where `kernel.trader.is_running` is always False — verify `StartupHealthCheckFailed` is raised after timeout with the diagnosis attached
3. Unit test `diagnose` with mock node where `data_engine.check_connected()` returns False — verify the diagnosis string contains `data_engine.check_connected()=False`
4. Unit test `diagnose` where `exec_engine._clients` is a dict with two mocks — verify each client's reconciliation_active AND is_connected appear in the output
5. Unit test `diagnose` where `exec_engine._clients` access raises — verify the error is captured and diagnose() does not crash
6. Unit test `_trading_node_subprocess` with all `nautilus_trader` imports mocked: verify the order is `connect_db` → **self-write pid** → heartbeat thread started → `node.build()` → `start_async` → `wait_until_ready` → `status='ready'`
7. **Pid self-write test (regression for Codex v5 P0)**: verify `live_node_processes.pid` is updated to `os.getpid()` in the subprocess BEFORE any other DB work, and BEFORE `node.build()`
8. **Heartbeat-during-build test**: simulate a `node.build()` that takes 30s; verify the heartbeat thread advances `last_heartbeat_at` at least 5 times during build
9. Unit test that `StartupHealthCheckFailed` causes status='failed' with the diagnosis as error_message, exit_code=2, and dispose() is called in finally
10. Unit test that an exception inside `node.run()` still triggers the finally block with status='failed', exit_code=1
11. Unit test that SIGTERM triggers `node.stop_async`
12. **Canonical signal test**: assert that `wait_until_ready` checks `node.kernel.trader.is_running`. Use a mock where the made-up attribute would return True but `kernel.trader.is_running` is False — verify the check correctly fails.
13. Implement

Acceptance: tests pass.

Effort: L
Depends on: 1.1, 1.1b, 1.5
Gotchas: #1 (uvloop), Codex v5 P0 (subprocess self-writes pid; watchdog is supervisor-side not asyncio.wait_for), Codex v5 P1 (diagnose uses private `_clients` dict because registered_clients is a list[ClientId]), decision #14 (canonical signal), decision #17 (heartbeat-before-build), #13 (manage_stop), #18 (asyncio.run), #20 (dispose)

---

#### 1.9 — Heartbeat task in subprocess

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (extend)
- `claude-version/backend/tests/integration/test_trading_node_heartbeat.py` (new)

A `threading.Thread` (NOT asyncio task — the trading node owns the event loop) that updates `live_node_processes.last_heartbeat_at = now()` every 5 seconds.

**Ordering** (decision #17, enforced in task 1.8): the heartbeat thread starts **BEFORE** `node.build()`, immediately after the subprocess writes `status='building'` and `pid=os.getpid()`. It runs continuously through build, through `start_async`, through `wait_until_ready`, and through `node.run()`. It is stopped in the `finally` block (before `node.stop_async` + `node.dispose`). v5's docstring for this task previously said "Started after `node.build()`" — that was a stale remnant from v4 and contradicted the actual ordering decision #17 / task 1.8 describe. v6 removes the contradiction.

Why a thread, not asyncio: writing to Postgres from inside Nautilus's event loop adds complexity (we'd need to share the loop). A short-lived sync DB write from a daemon thread is simpler and the heartbeat doesn't need low latency.

TDD:

1. Integration test with a stub subprocess (no actual TradingNode) that runs the heartbeat for 12 seconds, verifies `last_heartbeat_at` advances at least twice
2. **Ordering test**: assert the heartbeat thread is started BEFORE `node.build()` in the task 1.8 subprocess flow (use the unit test from 1.8)
3. Implement

Acceptance: integration test green.

Effort: S
Depends on: 1.1, 1.8
Gotchas: Codex v5 P2 — task 1.9 must NOT contradict decision #17 on ordering

---

#### 1.10 — Stop sequence via native `manage_stop = True`

Files:

- `claude-version/backend/src/msai/services/nautilus/trading_node.py` (SIGTERM handler already in 1.8)
- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (set `manage_stop=True` on StrategyConfig)
- `claude-version/backend/tests/integration/test_trading_node_stop.py` (new)

v2 had a custom `Strategy.on_stop` that called `cancel_all_orders` + `close_all_positions`. v3 deletes that and uses Nautilus's native `manage_stop = True` instead (Codex v2 P2).

With `manage_stop=True`, Nautilus runs the built-in market-exit loop (`trading/strategy.pyx:1779`) on strategy stop: it cancels all open orders for the strategy's instrument and submits market orders to close any open positions. No custom code.

```python
# In build_live_trading_node_config (1.5):
strategies=[
    ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=strategy_config_path,
        config={
            **strategy_config,
            "manage_stop": True,  # native flatten
            "order_id_tag": deployment_slug,  # deterministic
        },
    ),
]
```

The stop sequence is now:

1. Supervisor sends SIGTERM to the subprocess pid
2. Subprocess's signal handler updates `live_node_processes.status="stopping"` and schedules `node.stop_async()` on the kernel's event loop
3. Nautilus stops the strategy; because `manage_stop=True`, the built-in exit loop flattens positions and cancels orders
4. Subprocess exits cleanly, `finally` block writes `status="stopped"`, `exit_code=0`
5. If the subprocess does not exit within 30 seconds, the supervisor escalates to SIGKILL (ProcessManager.stop in 1.7 already handles this)

TDD:

1. Integration test: spawn subprocess with a stub strategy holding an open position, send SIGTERM, verify Nautilus closes the position via `manage_stop` (mocked broker records the close order), verify exit_code=0 and status="stopped"
2. Integration test: spawn a subprocess that ignores SIGTERM (e.g. blocking in a tight loop), verify the supervisor's SIGKILL escalation fires and status="failed"
3. Implement

Acceptance: tests pass.

Effort: S (dramatically simpler than v2)
Depends on: 1.7, 1.8
Gotchas: #13 (fixed by `manage_stop=True`, no custom code)

---

#### 1.11 — Order audit hook with `client_order_id` correlation

Files:

- `claude-version/backend/src/msai/services/nautilus/audit_hook.py` (new)
- `claude-version/backend/tests/unit/test_audit_hook.py` (new)

A Strategy mixin that intercepts order submissions:

```python
class AuditedStrategy(Strategy):
    def submit_order_with_audit(self, order: Order) -> None:
        client_order_id = order.client_order_id.value
        # Insert "submitted" row keyed by client_order_id BEFORE broker
        self._audit.write_submitted(
            client_order_id=client_order_id,
            deployment_id=self._deployment_id,
            strategy_id=self._strategy_id,
            strategy_code_hash=self._strategy_code_hash,  # from 1.13
            instrument_id=str(order.instrument_id),
            side=str(order.side),
            quantity=Decimal(str(order.quantity)),
            price=Decimal(str(order.price)) if hasattr(order, "price") else None,
            order_type=str(order.order_type),
            ts_attempted=now_utc(),
            status="submitted",
        )
        self.submit_order(order)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="accepted",
            broker_order_id=str(event.venue_order_id) if event.venue_order_id else None,
        )

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="rejected",
            reason=event.reason,
        )

    def on_order_filled(self, event: OrderFilled) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="filled",
        )

    def on_order_denied(self, event: OrderDenied) -> None:
        self._audit.update_status(
            client_order_id=event.client_order_id.value,
            status="denied",
            reason=event.reason,
        )
```

TDD:

1. Mock Strategy + DB; call `submit_order_with_audit`; verify "submitted" row written with `client_order_id`
2. Fire each event; verify the row is updated through the lifecycle by `client_order_id`
3. Test that `on_order_denied` records `denied` status
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.2, 1.13
Gotchas: Codex #7 (client_order_id correlation)

---

#### 1.12 — Strategy code hash from file bytes (NOT git)

Files:

- `claude-version/backend/src/msai/services/nautilus/strategy_hash.py` (new)
- `claude-version/backend/tests/unit/test_strategy_hash.py` (new)

```python
def compute_strategy_code_hash(strategy_path: Path) -> str:
    """SHA256 of the strategy file bytes. Used for reproducibility on
    every backtest and live deployment.

    Why not git rev-parse: Codex finding #7. The container only mounts
    src/ and strategies/, not the repo root. git is not available in
    the container at all.
    """
    return hashlib.sha256(strategy_path.read_bytes()).hexdigest()


def get_git_sha_from_env() -> str | None:
    """Read MSAI_GIT_SHA from env. Set by docker compose at build time
    via build args. Optional — used for traceability but not required.
    """
    return os.environ.get("MSAI_GIT_SHA")
```

The strategy hash is computed once at deploy time (in the API endpoint) and persisted on the `live_deployments` row. The audit hook (1.11) reads it from the row, doesn't recompute.

TDD: hash a known file, verify result matches OpenSSL.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex #7

---

#### 1.13 — `GET /api/v1/live/status/{deployment_id}` route

Files:

- `claude-version/backend/src/msai/api/live.py` (modify — add the parameterized route)
- `claude-version/backend/tests/unit/test_live_status_endpoint.py` (extend)

```python
@router.get("/status/{deployment_id}", response_model=LiveDeploymentStatusResponse)
async def get_live_deployment_status(
    deployment_id: UUID,
    db: AsyncSession = Depends(get_db),
    _claims: dict = Depends(get_current_user),
) -> LiveDeploymentStatusResponse:
    """Return the current status of a single live deployment.

    Reads from the `live_node_processes` table — does NOT maintain
    in-memory state. The supervisor + subprocess write to the table;
    this endpoint just reads.
    """
```

The existing `GET /api/v1/live/status` (no path param) returns all running deployments — keep it.

TDD:

1. Test the endpoint returns 404 for unknown deployment_id
2. Test it returns the row data for a known deployment
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.1
Gotchas: Codex #13 (route was missing)

---

#### 1.14 — Wire `/api/v1/live/start` and `/stop` to the command bus (with in-flight idempotency reservation)

Files:

- `claude-version/backend/src/msai/api/live.py` (modify start/stop endpoints)
- `claude-version/backend/src/msai/services/live/idempotency.py` (new — atomic SETNX reservation store)
- `claude-version/backend/tests/integration/test_live_start_stop_endpoints.py` (new)
- `claude-version/backend/tests/integration/test_live_start_idempotency.py` (new)

`POST /api/v1/live/start` is the request that publishes a start command. v4 had a post-hoc Idempotency-Key cache, but two concurrent retries with the same key could both miss the cache and both publish (Codex v4 P2). v5 reserves the slot atomically via `SET NX` BEFORE doing any work. The reservation is also user-scoped to eliminate cross-principal leak risk.

```python
@router.post("/start", status_code=201, response_model=LiveDeploymentResponse)
async def start_live_deployment(
    body: LiveDeploymentStartRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    redis: Redis = Depends(get_redis),
    idem: IdempotencyStore = Depends(get_idempotency_store),
    user: User = Depends(get_current_user),
) -> LiveDeploymentResponse:
    """Start (or re-start) a live deployment.

    Three idempotency layers (decision #13):

    1. HTTP Idempotency-Key (atomic in-flight reservation):
       - First request with a given key SETs a Redis key 'msai:idem:start:{user_id}:{key_hash}'
         with NX EX 60 → reserves the slot, value=PENDING
       - Concurrent retries get a 425 Too Early until the first one completes
       - On completion, the first request rewrites the key (no NX) with the
         cached response (status code + body) and TTL=86400 (24h)
       - A subsequent retry within the 24h TTL gets the cached response
       - Mismatch (same key, different body) returns 422
       - Key is USER-SCOPED (Codex v4 P2 — eliminates cross-principal leak)

    2. Halt-flag check (decision #16):
       - If msai:risk:halt is set, return 503 immediately

    3. Identity-based row lookup (decision #7, broader v5 tuple):
       - Compute identity_signature from the request
       - SELECT * FROM live_deployments WHERE identity_signature = :sig
       - Found: warm restart — reuse deployment_slug, trader_id, etc.
       - Not found: cold start — INSERT new row with new deployment_slug

    4. Active-process de-duplication:
       - If the existing deployment already has a live_node_processes row
         in (starting,building,ready,running), return 200 with the existing
         deployment_id WITHOUT publishing a new command

    Workflow (v8, all branches produce an EndpointOutcome,
    only the Reserved branch touches the idempotency store):

    A. If Idempotency-Key set: call idem.reserve(user_id, key, body_hash).
       Pattern-match on the result:
       - Reserved(redis_key):
           store_key = redis_key  # we own it
           (continue to step C)
       - InFlight():
           return EndpointOutcome.in_flight()  # NO store access
       - CachedOutcome(outcome=cached):
           return cached  # NO store access
       - BodyMismatchReservation():
           return EndpointOutcome.body_mismatch()  # NO store access
    B. No Idempotency-Key → store_key = None, skip layer 1
    C. Check halt flag: if set →
           outcome = EndpointOutcome.halt_active()  # cacheable=False
           jump to step N
    D. Compute identity_signature from the VALIDATED config model
       (derive_deployment_identity called with the Pydantic model)
    E. Look up live_deployments by identity_signature
       - Found: reuse row + deployment_slug (warm restart)
       - Not found: INSERT new row with new deployment_slug (cold start)
    F. Look up active live_node_processes
       - Active:
           outcome = EndpointOutcome.already_active(existing_deployment_id, ...)  # 200, cacheable=True
           jump to step N
       - Not active: continue
    G. Publish start command via LiveCommandBus.publish_start
    H. Poll live_node_processes for status='ready' or 'failed' with timeout (60s)
    I. On 'ready':
           outcome = EndpointOutcome.ready(deployment_id, body)  # 201, cacheable=True
    J. On 'failed', read row.failure_kind via FailureKind.parse_or_unknown(row.failure_kind):
       - SPAWN_FAILED_PERMANENT, RECONCILIATION_FAILED, BUILD_TIMEOUT, UNKNOWN →
             outcome = EndpointOutcome.permanent_failure(kind, row.error_message)  # 503, cacheable=True
       - HALT_ACTIVE →
             outcome = EndpointOutcome.halt_active()  # 503, cacheable=False
       - other/unexpected → treat as UNKNOWN (permanent failure)
    K. On poll timeout: outcome = EndpointOutcome.api_poll_timeout()  # 504, cacheable=False
    L. Any raised exception during the endpoint body → release reservation
       (if store_key is not None), re-raise to FastAPI

    Step N (final — only if store_key is not None, i.e. Reserved branch):
       if outcome.cacheable:
           await idem.commit(store_key, body_hash, outcome)
       else:
           await idem.release(store_key)
       return outcome

    v8 change (Codex v7 P0): only the Reserved branch of the reserve()
    result may call commit() or release(). The other branches return
    their outcome directly. This prevents a BodyMismatchReservation
    from overwriting the original correct cached response.

    Why `failure_kind` lives on the row (not the detail string):
      The subprocess (1.8) and the supervisor (1.7) both write
      `error_message` to live_node_processes, but each failure path
      ALSO sets a `failure_kind` column (StrEnum) so the endpoint can
      translate the row into an EndpointOutcome without parsing
      strings. The endpoint reads via FailureKind.parse_or_unknown()
      so stale/corrupted/NULL values degrade safely to UNKNOWN (treated
      as permanent failure, cacheable=True — human operator escalation).
    """
```

`idempotency.py` — EndpointOutcome + structured reservation store:

```python
from enum import StrEnum

# v6 TTL (unchanged in v7):
# - RESERVATION_TTL_S must cover the worst-case startup path:
#   build_timeout_s (120) + startup_health_timeout_s (60)
#   + api_poll_timeout_s (60) + margin = 300s.
# - RESPONSE_TTL_S stays at 24h for cacheable responses only.
RESERVATION_TTL_S = 300
RESPONSE_TTL_S = 86400  # 24 hours — cacheable responses only


class FailureKind(StrEnum):
    """Structured failure classification written on live_node_processes.failure_kind
    by the subprocess (1.8) and supervisor (1.7), and mirrored onto
    EndpointOutcome.failure_kind by /api/v1/live/start. This is the
    v7 replacement for v6's status-string parsing (Codex v6 P1)."""

    NONE = "none"                             # success path
    IN_FLIGHT = "in_flight"                   # another request holds the reservation (HTTP 425)
    HALT_ACTIVE = "halt_active"               # kill switch is set (HTTP 503, NOT cacheable)
    SPAWN_FAILED_PERMANENT = "spawn_failed_permanent"  # HTTP 503, cacheable
    RECONCILIATION_FAILED = "reconciliation_failed"    # HTTP 503, cacheable
    BUILD_TIMEOUT = "build_timeout"                    # HTTP 503, cacheable
    API_POLL_TIMEOUT = "api_poll_timeout"              # HTTP 504, NOT cacheable (retryable)
    BODY_MISMATCH = "body_mismatch"                    # HTTP 422, NOT cacheable (v8 — Codex v7 P0)
    UNKNOWN = "unknown"                       # v8: fallback for unrecognized DB values (Codex v7 P1)

    @classmethod
    def parse_or_unknown(cls, db_string: str | None) -> "FailureKind":
        """Safe parser: any unrecognized or NULL value maps to UNKNOWN.

        v8 addition (Codex v7 P1): the endpoint reads
        live_node_processes.failure_kind and must not crash on:
        - NULL (an older migration or a row never touched by a v8
          writer)
        - A value from a future schema version
        - A typo or corruption

        The endpoint should treat UNKNOWN like a permanent failure
        (cacheable=True, HTTP 503) so the operator sees the error
        in the cached response but retries with the same
        Idempotency-Key don't re-attempt automatically. The human
        operator is the escalation path.
        """
        if db_string is None:
            return cls.UNKNOWN
        try:
            return cls(db_string)
        except ValueError:
            return cls.UNKNOWN


@dataclass(slots=True, frozen=True)
class EndpointOutcome:
    """Structured endpoint outcome. Used by /api/v1/live/start to
    produce a response AND decide whether the idempotency layer
    should cache it.

    v7 change (Codex v6 P1): replaces v6's status-code-based
    `_TERMINAL_STATUSES` allowlist + string parsing. The endpoint
    branches each produce an EndpointOutcome, and the idempotency
    layer's `commit()` simply reads `outcome.cacheable`. No code
    inspects `status_code` to decide cacheability.
    """

    status_code: int
    response: dict
    cacheable: bool                            # True → commit, False → release
    failure_kind: FailureKind = FailureKind.NONE

    @classmethod
    def ready(cls, deployment_id: UUID, body: dict) -> "EndpointOutcome":
        return cls(status_code=201, response=body, cacheable=True)

    @classmethod
    def already_active(cls, deployment_id: UUID, body: dict) -> "EndpointOutcome":
        # v7 fix: 200, not 201 (v6 workflow had 200/201 mismatch)
        return cls(status_code=200, response=body, cacheable=True)

    @classmethod
    def halt_active(cls) -> "EndpointOutcome":
        return cls(
            status_code=503,
            response={"detail": "Kill switch is active. POST /api/v1/live/resume to clear."},
            cacheable=False,
            failure_kind=FailureKind.HALT_ACTIVE,
        )

    @classmethod
    def in_flight(cls) -> "EndpointOutcome":
        return cls(
            status_code=425,
            response={"detail": "Another request with the same Idempotency-Key is in flight."},
            cacheable=False,
            failure_kind=FailureKind.IN_FLIGHT,
        )

    @classmethod
    def api_poll_timeout(cls) -> "EndpointOutcome":
        return cls(
            status_code=504,
            response={"detail": "Deployment did not reach 'ready' within the poll timeout."},
            cacheable=False,
            failure_kind=FailureKind.API_POLL_TIMEOUT,
        )

    @classmethod
    def permanent_failure(
        cls,
        row_failure_kind: FailureKind,
        error_message: str,
    ) -> "EndpointOutcome":
        """Build a cacheable 503 from a row's failure_kind.

        v8: accepts UNKNOWN in addition to the known permanent kinds.
        UNKNOWN comes from FailureKind.parse_or_unknown() when the
        database row has a NULL, stale, or corrupted value — the
        endpoint treats it as a permanent failure (cacheable=True)
        with the human-readable error_message so the operator can
        investigate.
        """
        assert row_failure_kind in {
            FailureKind.SPAWN_FAILED_PERMANENT,
            FailureKind.RECONCILIATION_FAILED,
            FailureKind.BUILD_TIMEOUT,
            FailureKind.UNKNOWN,  # v8 (Codex v7 P1)
        }
        return cls(
            status_code=503,
            response={"detail": error_message, "failure_kind": row_failure_kind.value},
            cacheable=True,
            failure_kind=row_failure_kind,
        )

    @classmethod
    def body_mismatch(cls) -> "EndpointOutcome":
        """v8 fix (Codex v7 P0): cacheable=False.

        A body-mismatch response means the caller does NOT own the
        reservation slot — another request already holds it. Caching
        this 422 would overwrite the original correct response at the
        same key, poisoning all subsequent correct retries. The
        endpoint must NOT call commit() on this outcome; the correct
        dispatch is 'return this outcome without touching the store'.

        The endpoint code path uses pattern matching on the
        `reserve()` result:

            match reservation:
                case Reserved(redis_key=redis_key):
                    # we own the slot; may commit or release
                    ...
                case BodyMismatchReservation():
                    return EndpointOutcome.body_mismatch()  # no store access
                case CachedOutcome(outcome=cached):
                    return cached  # no store access
                case InFlight():
                    return EndpointOutcome.in_flight()  # no store access

        Only the Reserved branch is allowed to call commit() / release().
        """
        return cls(
            status_code=422,
            response={"detail": "Idempotency-Key reused with a different request body."},
            cacheable=False,  # v8: NOT cacheable (Codex v7 P0)
            failure_kind=FailureKind.BODY_MISMATCH,
        )


class IdempotencyStore:
    """Redis-backed Idempotency-Key store with atomic in-flight reservation.

    Key format: msai:idem:start:{user_id}:{sha256(idempotency_key)}

    States:
    - Missing: no prior request with this key
    - PENDING: another request is in flight with this key (reserved via SETNX)
    - <serialized EndpointOutcome>: a prior request completed with a cacheable outcome

    v7 changes (Codex v6 P1):
    - commit() takes an EndpointOutcome and reads outcome.cacheable.
      No status-code allowlist. No ValueError.
    - Transient outcomes still call release() so retries can re-attempt.
    - The endpoint's final step is a single branch:
        if outcome.cacheable: await idem.commit(key, outcome)
        else:                 await idem.release(key)
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(user_id: UUID, key: str) -> str:
        h = hashlib.sha256(key.encode()).hexdigest()
        return f"msai:idem:start:{user_id.hex}:{h}"

    async def reserve(
        self,
        user_id: UUID,
        key: str,
        body_hash: str,
    ) -> "ReservationResult":
        """Atomic SETNX reservation. Returns one of:
        - Reserved(redis_key) → proceed, caller must eventually call commit() or release()
        - InFlight → the endpoint returns EndpointOutcome.in_flight()
        - CachedOutcome(outcome) → the endpoint returns the cached outcome unchanged
        - BodyMismatch → the endpoint returns EndpointOutcome.body_mismatch()
        """
        redis_key = self._key(user_id, key)
        marker = msgpack.packb({
            "state": "pending",
            "body_hash": body_hash,
            "at": utcnow().isoformat(),
        })
        # SET NX EX — atomic reserve. Returns True if set, None if key already exists.
        was_set = await self._redis.set(redis_key, marker, nx=True, ex=RESERVATION_TTL_S)
        if was_set:
            return Reserved(redis_key=redis_key)
        existing = await self._redis.get(redis_key)
        if existing is None:
            # Race: key expired between SET NX and GET. Retry once.
            return await self.reserve(user_id, key, body_hash)
        decoded = msgpack.unpackb(existing)
        if decoded.get("state") == "pending":
            return InFlight()
        # Cached outcome
        if decoded.get("body_hash") != body_hash:
            return BodyMismatchReservation()
        outcome = EndpointOutcome(
            status_code=decoded["outcome"]["status_code"],
            response=decoded["outcome"]["response"],
            cacheable=decoded["outcome"]["cacheable"],
            failure_kind=FailureKind(decoded["outcome"]["failure_kind"]),
        )
        return CachedOutcome(outcome=outcome)

    async def commit(
        self,
        redis_key: str,
        body_hash: str,
        outcome: EndpointOutcome,
    ) -> None:
        """Cache the outcome for 24h. v7 replacement for commit_terminal.

        No status-code allowlist — the outcome itself declares
        whether it's cacheable. The endpoint must only call commit()
        when outcome.cacheable == True. Calling with a non-cacheable
        outcome raises (programming error — the endpoint should have
        called release() instead).
        """
        if not outcome.cacheable:
            raise ValueError(
                f"commit() called with a non-cacheable outcome "
                f"(status={outcome.status_code}, failure_kind={outcome.failure_kind}). "
                f"Use release() for transient outcomes."
            )
        payload = msgpack.packb({
            "state": "completed",
            "body_hash": body_hash,
            "outcome": {
                "status_code": outcome.status_code,
                "response": outcome.response,
                "cacheable": outcome.cacheable,
                "failure_kind": outcome.failure_kind.value,
            },
            "at": utcnow().isoformat(),
        })
        await self._redis.set(redis_key, payload, ex=RESPONSE_TTL_S)

    async def release(self, redis_key: str) -> None:
        """Release the reservation. Called on:
        - Transient outcomes (IN_FLIGHT, HALT_ACTIVE, API_POLL_TIMEOUT)
        - Hard failures (raised exceptions, bugs)
        After release, the next retry with the same key will SETNX-reserve
        a fresh slot.
        """
        await self._redis.delete(redis_key)


@dataclass(slots=True, frozen=True)
class CachedOutcome:
    outcome: EndpointOutcome


@dataclass(slots=True, frozen=True)
class BodyMismatchReservation: ...

# `Reserved` and `InFlight` dataclasses unchanged from v6.


@dataclass(slots=True, frozen=True)
class Reserved:
    redis_key: str


@dataclass(slots=True, frozen=True)
class InFlight: ...


@dataclass(slots=True, frozen=True)
class Cached:
    response: dict
    status_code: int


@dataclass(slots=True, frozen=True)
class BodyMismatch: ...


ReservationResult = Reserved | InFlight | Cached | BodyMismatch
```

The endpoint translates the `ReservationResult` to HTTP:

- `Reserved` → proceed; on completion call `idem.commit(...)`; on hard exception call `idem.release(...)`
- `InFlight` → 425 Too Early
- `Cached` → return the cached response with the cached status code
- `BodyMismatch` → 422 Unprocessable Entity

`POST /api/v1/live/stop`:

```python
@router.post("/stop")
async def stop_live_deployment(
    body: LiveDeploymentStopRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    _user: User = Depends(get_current_user),
):
    """Stop a running deployment.

    Idempotent: if no active live_node_processes row exists, return 200
    immediately (already stopped). Otherwise publish a stop command and
    poll for status in (stopped, failed) with a 60s timeout.

    Idempotency-Key handling identical to /start.
    """
```

TDD:

1. **EndpointOutcome factory test (v7/v8/v9)**: construct each factory method (`ready`, `already_active`, `halt_active`, `in_flight`, `api_poll_timeout`, `permanent_failure`, `body_mismatch`) and assert the `status_code`, `cacheable`, and `failure_kind` fields are correct:
   - `ready` → 201, cacheable=True, NONE
   - `already_active` → 200 (NOT 201), cacheable=True, NONE — regression for Codex v6 P1 status-code mismatch
   - `halt_active` → 503, cacheable=**False**, HALT_ACTIVE
   - `in_flight` → 425, cacheable=False, IN_FLIGHT
   - `api_poll_timeout` → 504, cacheable=False, API_POLL_TIMEOUT
   - `permanent_failure(SPAWN_FAILED_PERMANENT, ...)` → 503, cacheable=**True**, SPAWN_FAILED_PERMANENT
   - `permanent_failure(UNKNOWN, ...)` → 503, cacheable=True, UNKNOWN (v8)
   - `body_mismatch` → 422, cacheable=**False**, BODY_MISMATCH (v8 — Codex v7 P0 fix; caller does NOT own the reservation, so the outcome must be non-cacheable to prevent poisoning the original cached response)
2. **commit() rejects non-cacheable (v7)**: construct `EndpointOutcome.halt_active()`, call `idem.commit(key, outcome)`, assert it raises `ValueError` (programming error — should have called release)
3. **Reserved-only commit enforcement (v8/v9, Codex v7 P0)**: pattern-match test verifying that the endpoint code NEVER calls `commit()` or `release()` from the `InFlight`, `CachedOutcome`, or `BodyMismatchReservation` branches. Use a spy on `idem.commit` / `idem.release` and assert zero calls from those three branches across all test inputs.
4. Integration test: `/start` with no `Idempotency-Key` for a fresh strategy → publishes, mocked supervisor flips to ready, returns 201
5. Integration test: `/start` twice with the SAME `Idempotency-Key` and identical body → second call returns the cached outcome via `CachedOutcome`
6. Integration test: `/start` twice with the SAME `Idempotency-Key` but different body → second returns 422 (BodyMismatchReservation)
7. **In-flight race test (regression for Codex v4 P2)**: launch two concurrent `/start` requests with the SAME `Idempotency-Key` (`asyncio.gather`); assert exactly one wins SETNX and the other gets 425 `IN_FLIGHT`; assert exactly one publish command is sent
8. **User-scoping test**: two different users send `/start` with the SAME `Idempotency-Key` value AND different bodies; assert both succeed
9. **Halt-flag outcome (v7, regression for Codex v6 P1)**: set `msai:risk:halt`, call `/start` → returns 503 with `failure_kind=HALT_ACTIVE`; verify `idem.release()` IS called and `idem.commit()` is NOT; subsequent retry with the same key re-attempts (the reservation was released, not cached)
10. **Permanent-failure outcome (v7)**: mock the subprocess to write `failure_kind='spawn_failed_permanent'` to the row; call `/start`; verify it returns 503 with `failure_kind=SPAWN_FAILED_PERMANENT` AND `idem.commit()` IS called; subsequent retry with the same key returns the CACHED 503 without re-attempting
11. **API poll timeout (v7)**: mock the poll to never observe `ready`; verify `/start` returns 504 with `failure_kind=API_POLL_TIMEOUT`; verify `idem.release()` is called; subsequent retry re-attempts
12. **failure_kind sourced from row, not string parsing (v7, regression for Codex v6 P1)**: seed a row with `failure_kind='reconciliation_failed'` and `error_message='whatever human-readable string'`; call `/start`; verify the endpoint reads the enum column, NOT the error_message string, to decide cacheability
13. **Reservation release on raised exception**: patch `bus.publish_start` to raise; verify `idem.release(redis_key)` is called in the handler's exception path; a subsequent retry with the SAME key re-attempts
14. Integration test: `/start` twice without `Idempotency-Key` for the same identity_signature while the first is still running → second returns 200 `already_active` (NOT 201) with the existing deployment_id and does NOT publish a new command
15. Integration test: `/start` for a previously stopped deployment with the SAME identity_signature → reuses the existing live_deployments row (same `deployment_slug` — warm restart)
16. Integration test: `/start` for the same strategy with a CHANGED config → produces a different `identity_signature`, inserts a new row with a fresh `deployment_slug` (cold start)
17. Integration test: stop endpoint publishes, mocked supervisor flips status to stopped
18. Integration test: stop endpoint when no active row exists → returns 200 immediately
19. Implement

Acceptance: tests pass.

Effort: M
Depends on: 1.1 (needs the `failure_kind` column on live_node_processes), 1.1b, 1.6, 1.13
Gotchas: Codex v3 P0 (HTTP idempotency), Codex v4 P2 (SETNX reservation, user-scoped key), Codex v6 P1 (structured EndpointOutcome + FailureKind enum; no status-code allowlists, no string parsing, no 200-vs-201 mismatch)

---

#### 1.15 — Deterministic smoke strategy

Files:

- `claude-version/strategies/example/smoke_market_order.py` (new)
- `claude-version/backend/tests/unit/test_smoke_strategy.py` (new)

```python
class SmokeMarketOrderConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    manage_stop: bool = True  # v3 decision #11: native flatten on stop
    order_id_tag: str = ""    # v3 decision #7: injected from deployment_slug


class SmokeMarketOrderStrategy(AuditedStrategy):
    """Submits exactly ONE tiny market order on the first bar received,
    then sits idle. Used by the Phase 1 E2E to prove the order path
    end-to-end.

    Why: the EMA strategy may not cross during a short E2E window
    (Codex finding #8). The smoke strategy is deterministic.

    No custom on_stop — `manage_stop=True` on the config tells Nautilus
    to cancel all open orders and flatten positions automatically when
    the strategy is stopped (v3 decision #11).
    """

    def __init__(self, config: SmokeMarketOrderConfig) -> None:
        super().__init__(config=config)
        self._order_submitted = False

    def on_bar(self, bar: Bar) -> None:
        if self._order_submitted:
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("1"),
        )
        self.submit_order_with_audit(order)
        self._order_submitted = True
```

TDD:

1. Unit test: feed a synthetic bar, verify exactly one order is submitted
2. Feed a second bar, verify NO additional order is submitted
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.11
Gotchas: Codex #8

---

#### 1.16 — Phase 1 E2E verification harness

Files:

- `claude-version/backend/tests/e2e/test_live_trading_phase1.py` (new)
- `claude-version/scripts/e2e_phase1.sh` (helper)

Docker-based E2E:

1. `docker compose -f docker-compose.dev.yml up -d`
2. IB Gateway paper container with credentials from env
3. POST `/api/v1/live/start` with the smoke strategy and `instruments=["AAPL"]`
4. Assert response is 201, get deployment_id
5. Verify `live_node_processes` heartbeat advances by ≥2 over 12 seconds
6. Wait for at least one bar to arrive (poll for an audit row)
7. Verify the audit table has exactly 1 row with `status` in `(submitted, accepted, filled)`
8. Verify the row has `client_order_id`, `strategy_code_hash`, `instrument_id="AAPL.NASDAQ"` (or whatever the IB provider returns), `side="BUY"`, `quantity=1`
9. **Kill the FastAPI container**: `docker kill msai-claude-backend`
10. Sleep 5s
11. `docker compose up -d backend`
12. Verify the trading subprocess is still alive (heartbeat still advancing)
13. `GET /api/v1/live/status/{deployment_id}` returns the running deployment from the registry
14. POST `/api/v1/live/stop`
15. Verify `live_node_processes.status="stopped"`, `exit_code=0`
16. Verify the IB account has zero open positions for the instrument

Gated by `MSAI_E2E_IB_ENABLED=1`.

Acceptance: harness passes locally against a real IB Gateway paper container.

Effort: L
Depends on: 1.1–1.15
Gotchas: covered

---

### Phase 1 task ordering

These tasks must run sequentially because later ones edit files earlier ones create:

```
1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6 → 1.7 → 1.8 → 1.9 → 1.10 → 1.11 → 1.12 → 1.13 → 1.14 → 1.15 → 1.16
```

There is no parallelization in Phase 1. Codex finding #13 was correct: 1.7/1.8/1.9/1.10/1.11 all hot-edit `trading_node.py` and `audit_hook.py`. The earlier "Group D parallelizable" claim was wrong.

---

## Phase 2 — Security Master + Catalog Migration + Parity

**Goal:** Backtest and live use the same canonical instruments. The fake `TestInstrumentProvider.equity(SIM)` is gone. Multi-asset support actually works.

**Phase 2 acceptance:**

- A backtest of `AAPL.NASDAQ` uses real IB contract details from the SecurityMaster cache
- A live deployment of `AAPL` resolves to the **exact same** `AAPL.NASDAQ` `Instrument` object
- The parity validation harness runs the EMA strategy in both backtest and historical-paper-replay over the same window and asserts intent-level parity (see 2.11)
- The streaming catalog builder handles a 1 GB Parquet directory without OOM
- Existing `*.SIM` backtests are migrated to canonical IDs by a one-shot script
- The `instrument_cache` table stores `trading_hours` metadata so Phase 4's market-hours guard has something to read

### Phase 2 tasks

#### 2.1 — `InstrumentSpec` dataclass

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/specs.py` (new)
- `claude-version/backend/tests/unit/test_instrument_spec.py` (new)

```python
@dataclass(slots=True, frozen=True)
class InstrumentSpec:
    asset_class: Literal["equity", "future", "option", "forex", "index"]
    symbol: str
    venue: str
    currency: str = "USD"
    expiry: date | None = None
    strike: Decimal | None = None
    right: Literal["C", "P"] | None = None
    underlying: str | None = None
    multiplier: Decimal | None = None

    def canonical_id(self) -> str:
        """Return the IB_SIMPLIFIED canonical Nautilus instrument ID string."""
```

TDD: per-asset-class canonical_id tests; bad combinations raise ValueError.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: #4 (venue suffix discipline)

---

#### 2.2 — Postgres `instrument_cache` table with `trading_hours`

Files:

- `claude-version/backend/src/msai/models/instrument_cache.py` (new)
- `claude-version/backend/alembic/versions/<rev>_add_instrument_cache.py` (new)
- `claude-version/backend/tests/integration/test_instrument_cache_model.py` (new)

```python
class InstrumentCache(Base, TimestampMixin):
    __tablename__ = "instrument_cache"
    canonical_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ib_contract_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    nautilus_instrument_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    trading_hours: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Schema: {"timezone": "America/New_York", "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}, ...], "eth": [...]}
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

`trading_hours` is populated by 2.4 from the IB contract details. Phase 4 task 4.3 reads it for the market-hours guard. Codex finding #9 — the dependency is now explicit.

TDD: integration test pattern.

Acceptance: tests pass.

Effort: S
Depends on: nothing
Gotchas: Codex #9

---

#### 2.3 — IB qualification adapter

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py` (new)
- `claude-version/backend/tests/unit/test_ib_qualifier.py` (new)

```python
class IBQualifier:
    """Wraps Nautilus's InteractiveBrokersInstrumentProvider to qualify
    InstrumentSpec → IBContract via the running TradingNode's IB connection.

    For the SecurityMaster service, we don't open our own IB connection —
    we delegate to a temporary InteractiveBrokersInstrumentProvider built
    on top of an isolated InteractiveBrokersClient. Throttles to ≤50 msg/sec
    to respect IB API limits.

    For continuous futures, uses CONTFUT secType. For options, uses
    reqSecDefOptParamsAsync (NOT reqContractDetails) to avoid throttling
    on chain queries.
    """

    async def qualify(self, spec: InstrumentSpec) -> Contract: ...
    async def qualify_many(self, specs: list[InstrumentSpec]) -> list[Contract]: ...
    async def front_month_future(self, root_symbol: str, exchange: str) -> Contract: ...
    async def option_chain(self, underlying: str, exchange: str, max_strikes: int) -> list[Contract]: ...
```

TDD:

1. Mock `ib_async.IB`, verify the right contract type per asset class
2. Test throttling with a fake clock
3. Test that an unqualified contract raises a clear error
4. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.1
Gotchas: #11 (don't load on critical path), #12 (option chains)

---

#### 2.4 — Nautilus instrument parser + trading hours extractor

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/parser.py` (new)
- `claude-version/backend/tests/unit/test_security_master_parser.py` (new)

Wraps Nautilus's `parse_instrument` (`adapters/interactive_brokers/parsing/instruments.py`) to return `Equity` / `FuturesContract` / `OptionContract` / `CurrencyPair`. Also extracts `trading_hours` from the IB `ContractDetails.tradingHours` and `liquidHours` strings into the JSONB schema documented in 2.2.

TDD:

1. Test that an `Equity` `IBContractDetails` parses to `Equity` with the right precision
2. Test trading_hours extraction for AAPL (NYSE hours) and ESM5 (CME hours, near-24h)
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.1, 2.3
Gotchas: none

---

#### 2.5 — `SecurityMaster` service

Files:

- `claude-version/backend/src/msai/services/nautilus/security_master/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/security_master/service.py` (new)
- `claude-version/backend/tests/unit/test_security_master.py` (new)

```python
class SecurityMaster:
    def __init__(self, qualifier: IBQualifier, parser: NautilusInstrumentParser, db: AsyncSession): ...

    async def resolve(self, spec_or_symbol: InstrumentSpec | str) -> Instrument:
        """Cache-first resolve. Order:
        1. Read from instrument_cache by canonical_id
        2. Miss: qualify via IBQualifier, parse via NautilusInstrumentParser,
           extract trading_hours, write to cache, return
        3. Stale: refresh in background, return cached for now
        """

    async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]: ...
    async def refresh(self, canonical_id: str) -> Instrument: ...

    @classmethod
    def shorthand_to_spec(cls, symbol: str) -> InstrumentSpec:
        """Best-effort shorthand: 'AAPL' → equity AAPL.NASDAQ."""
```

TDD:

1. Cache hit
2. Cache miss → qualify + parse + write + return
3. Bulk resolve uses batched calls
4. Shorthand for each asset class
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.2, 2.3, 2.4
Gotchas: #11

---

#### 2.6 — Replace `instruments.py` with SecurityMaster delegation

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (rewrite)
- `claude-version/backend/tests/unit/test_instruments.py` (rewrite)

Remove the `*.SIM` rebinding (`instruments.py:45` per architecture review). Delegate to `SecurityMaster.resolve()`.

A temporary `legacy_resolve_sim(symbol)` shim is kept for existing backtest test fixtures, marked deprecated, removed in 2.10.

TDD:

1. `resolve_instrument("AAPL")` returns an `Equity` with `instrument_id = "AAPL.NASDAQ"`
2. The instrument is structurally identical to what SecurityMaster returns
3. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.5
Gotchas: #4

---

#### 2.7 — Streaming catalog builder

Files:

- `claude-version/backend/src/msai/services/nautilus/catalog_builder.py` (modify)
- `claude-version/backend/tests/unit/test_catalog_builder_streaming.py` (new)

Replace the full-partition pandas load with `pyarrow.parquet.ParquetFile.iter_batches(batch_size=100_000)`. Each batch is wrangled via `BarDataWrangler` and appended to the catalog.

TDD:

1. Synthetic 1M-row Parquet file
2. Run new builder with `batch_size=100_000`
3. Assert peak memory ≤ 200 MB via `tracemalloc`
4. Assert resulting catalog has 1M bars
5. Existing tests still pass

Acceptance: tests pass.

Effort: M
Depends on: nothing
Gotchas: #15-adjacent (large catalogs need streaming, not batch)

---

#### 2.8 — Migration script: rebuild existing catalogs under canonical IDs

Files:

- `claude-version/scripts/migrate_catalog_to_canonical.py` (new — note: under `claude-version/scripts/`, not `backend/scripts/` per Codex finding #13)
- `claude-version/backend/tests/integration/test_migrate_catalog.py` (new)

Walks `data/parquet/<asset_class>/<symbol>/`, resolves each via `SecurityMaster.shorthand_to_spec(symbol).canonical_id()`, builds Nautilus catalog under `data/nautilus/<canonical_id>/`. Idempotent.

TDD:

1. Synthetic input
2. Run migration
3. Assert output exists
4. Re-run is no-op
5. Implement

Acceptance: tests pass.

Effort: M
Depends on: 2.5, 2.7
Gotchas: Codex #13 (script location)

---

#### 2.9 — Update backtest API + worker for canonical IDs

Files:

- `claude-version/backend/src/msai/api/backtests.py` (modify)
- `claude-version/backend/src/msai/workers/backtest_job.py` (modify)
- `claude-version/backend/tests/unit/test_backtests_api.py` (modify)

`POST /api/v1/backtests/run` accepts shorthand or canonical; resolves shorthand via `SecurityMaster.shorthand_to_spec`; persists canonical IDs in `backtests.instruments`. The worker reads canonical only.

The backtest_runner builds a `BacktestVenueConfig` per unique venue in the instruments list (multiple venue configs if instruments span venues).

TDD:

1. POST with shorthand → row has canonical
2. POST with canonical → unchanged
3. Worker builds the right venue configs
4. Implement

Acceptance: tests pass; existing backtests run end-to-end producing the same trades under canonical IDs.

Effort: M
Depends on: 2.5, 2.6, 2.8
Gotchas: #4, #2

---

#### 2.10 — Remove `legacy_resolve_sim` shim

Files:

- `claude-version/backend/src/msai/services/nautilus/instruments.py` (delete shim)
- All `*.SIM`-dependent fixtures migrated

TDD: full test suite passes without the shim.

Acceptance: `git grep -l "legacy_resolve_sim"` returns nothing.

Effort: S
Depends on: 2.6, 2.9
Gotchas: none

---

#### 2.11 — Parity validation harness (redesigned for v3)

Files:

- `claude-version/scripts/parity_check.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/normalizer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/parity/comparator.py` (new)
- `claude-version/backend/tests/integration/test_parity_determinism.py` (new)
- `claude-version/backend/tests/integration/test_parity_config_roundtrip.py` (new)

v2 planned "feed bars into a TradingNode against IB paper" — this doesn't exist in Nautilus (Codex v2 P1). There is no Nautilus mode that runs `TradingNode` with IB paper exec + local catalog data replay. v3 replaces the harness with three tractable tests.

**Test A — Determinism test (backtest twice, same bars, same trades):**

The real risk the parity harness catches is strategy non-determinism — a strategy that depends on wall-clock time, random seeds without a fixed seed, or dict iteration order can drift between backtest runs. v3's determinism test runs the same strategy on the same Parquet catalog twice via `BacktestNode` and asserts the resulting trade list is byte-identical.

```python
def test_backtest_is_deterministic() -> None:
    result_a = run_backtest(strategy_path=..., catalog_path=..., window=...)
    result_b = run_backtest(strategy_path=..., catalog_path=..., window=...)
    assert normalize(result_a.trades) == normalize(result_b.trades)
```

**Test B — Config round-trip test (catches type errors between backtest and live configs):**

`ImportableStrategyConfig` is the Nautilus abstraction that loads a strategy class + config in both backtest and live. If the backtest strategy config and the live strategy config diverge in schema (e.g., an optional field added on one side), live deployments fail at instantiation. The round-trip test loads the strategy via `ImportableStrategyConfig` with the **live** config schema and asserts instantiation succeeds, even when run from a backtest context.

```python
def test_live_config_instantiates_in_backtest_context() -> None:
    live_config = build_live_strategy_config(...)
    importable = ImportableStrategyConfig(
        strategy_path=..., config_path=..., config=live_config
    )
    # Nautilus resolves and instantiates it via the BacktestNode path
    node = BacktestNode(configs=[build_backtest_run_config(...importable...)])
    node.build()  # Must not raise
    node.dispose()
```

**Test C — Intent capture contract (documentation, not a test):**

The real contract between backtest and live is that the strategy emits the same `(timestamp, instrument_id, side, signed_qty)` tuples given the same bars. The plan documents the `OrderIntent` dataclass:

```python
@dataclass(slots=True, frozen=True)
class OrderIntent:
    decision_timestamp: datetime  # bar-close timestamp
    instrument_id: str            # canonical Nautilus ID
    side: Literal["BUY", "SELL"]
    signed_qty: Decimal           # positive for buys, negative for sells
```

The `normalizer.py` module extracts `OrderIntent` tuples from a backtest `BacktestResult` (the list of submitted orders with timestamps). The `comparator.py` module compares two `list[OrderIntent]` sequences for exact ordered equality.

Both the backtest runner and the live audit hook (1.11) write `OrderIntent` records to disk (via the `order_attempt_audits` table). This lets us do **backtest-vs-production comparison after the fact**:

- Phase 5 paper soak produces a log of live `OrderIntent` tuples
- Operator can re-run the same strategy + same config against the same Parquet window in backtest
- Compare the two intent sequences for drift

**Why this is better than v2:**

- It's actually achievable with existing Nautilus APIs
- Determinism is the real risk the harness catches — wall-clock drift, RNG, dict order
- Config round-trip catches schema drift between backtest and live configs before deployment
- The intent contract is a stable artifact that lives across backtest and paper soak
- The paper soak in Phase 5 is what catches live-only divergence (latency, slippage) — the harness doesn't pretend to catch it

**Non-goals for v3 parity harness:**

- Compare against paper IB live fills (not achievable with stock Nautilus without a custom data feeder — deferred to a future phase if needed)
- Catch runtime divergence from latency/slippage (that's the paper soak's job)

TDD:

1. Unit test the normalizer: convert a `BacktestResult.orders_df` to `list[OrderIntent]`, verify round-trip
2. Unit test the comparator: feed two lists with known diffs (extra/missing/reordered decisions), verify the right errors
3. Integration test A (determinism): run the EMA strategy twice on a 1-day AAPL window, assert identical trades
4. Integration test B (config round-trip): load the live EMA config via `ImportableStrategyConfig` in a `BacktestNode`, assert instantiation succeeds

Acceptance: all four tests pass.

Effort: M (smaller than v2 because we dropped the IB paper leg)
Depends on: 2.5, 2.6, 2.9
Gotchas: #14 (divergence from fills — acknowledged and deferred to paper soak)

---

#### 2.12 — Multi-asset support

Three sub-tasks (parallelizable):

**2.12a — Futures**: extend specs/qualifier/parser. Front-month resolution via CONTFUT.
**2.12b — Options**: extend specs/qualifier/parser. Use `reqSecDefOptParamsAsync`. Require explicit strike (gotcha #12).
**2.12c — FX**: extend specs/qualifier. IDEALPRO venue.

Each: TDD pattern; tests cover one happy path + one edge case.

Effort: M each
Depends on: 2.5
Gotchas: #12

---

#### 2.13 — Phase 2 E2E

Files: `claude-version/backend/tests/e2e/test_security_master_phase2.py` (new)

E2E: start stack with paper IB Gateway; resolve `AAPL`, `ESM5.XCME`, `EUR/USD.IDEALPRO` via SecurityMaster API; run a backtest with `AAPL.NASDAQ` for a 1-day window; run parity harness; assert parity passes; verify streaming catalog builder peak memory ≤ 500 MB.

Effort: L
Depends on: 2.1–2.12

---

### Phase 2 task ordering / parallelization

```
2.1, 2.2, 2.7  (parallel — no inter-deps)
  ↓
2.3, 2.4 (parallel, both depend on 2.1)
  ↓
2.5 (depends on 2.2, 2.3, 2.4)
  ↓
2.6, 2.8, 2.9 (parallel, depend on 2.5; 2.8 also on 2.7)
  ↓
2.10, 2.11 (parallel, depend on 2.6, 2.9)
  ↓
2.12a, 2.12b, 2.12c (parallel, depend on 2.5)
  ↓
2.13 (depends on all)
```

---

## Phase 3 — Redis State Spine + Projection Layer + Risk in Order Path

**Goal:** The API can see what live strategies are doing in real-time, via Nautilus's own message bus published to Redis Streams. Risk runs on real position state. The kill switch actually closes positions.

**Phase 3 acceptance:**

- A live deployment publishes events through Nautilus's `MessageBusConfig.database = redis` to a **single** Redis Stream per trader, with a deterministic stream name `trader-MSAI-{deployment_slug}-stream` (v3 decision #8)
- A FastAPI projection consumer reads that stream via **consumer groups** (durable, no event loss on FastAPI restart)
- The consumer translates Nautilus events to a stable internal schema and publishes them to a **Redis pub/sub channel** per deployment (`msai:live:events:{deployment_id}`) — v3 decision #9, so multi-worker uvicorn still fans out correctly
- Every uvicorn worker subscribes to that pub/sub channel and pushes events to its own WebSocket clients
- The `/live` page shows real-time positions, fills, and PnL
- The `RiskAwareStrategy` mixin blocks an order that would breach a per-strategy max position, using the Nautilus `Portfolio` API inside the Strategy (`self.portfolio.account()`, `self.portfolio.net_exposure()`, `self.portfolio.total_pnl()`), which is populated automatically via `CacheConfig.database = redis`
- FastAPI reads position snapshots for the UI via the Nautilus **Cache Python API** (a transient `Cache` pointed at the same Redis backend — v3 decision #10), NOT by parsing raw Nautilus Redis keys
- `POST /api/v1/live/kill-all` sets a sticky halt flag in Redis that the strategy mixin reads on every `on_bar`

### Phase 3 tasks

#### 3.1 — Configure `CacheConfig.database = redis` for live (NOT backtest)

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/src/msai/services/nautilus/backtest_runner.py` (verify NO database config)
- `claude-version/backend/tests/unit/test_live_node_config_cache.py` (new)

```python
cache=CacheConfig(
    database=DatabaseConfig(
        type="redis",
        host=settings.redis_host,
        port=settings.redis_port,
    ),
    encoding="msgpack",
    buffer_interval_ms=None,  # write-through; gotcha #7. Codex #3 — must be None, not 0
    persist_account_events=True,
)
```

Backtest config has NO `cache.database` set (gotcha #8 inverse).

TDD:

1. Live config has `cache.database.type == "redis"` and `buffer_interval_ms is None`
2. Backtest config has `cache.database is None`
3. Implement

Acceptance: tests pass.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, Codex #3

---

#### 3.2 — Configure `MessageBusConfig.database = redis` for live (NOT backtest)

Files: same as 3.1 plus tests

```python
message_bus=MessageBusConfig(
    database=DatabaseConfig(type="redis", host=..., port=...),
    encoding="msgpack",          # gotcha #17 — JSON fails on Decimal/datetime/Path
    stream_per_topic=False,      # v3 decision #8 — ONE stream per trader
    use_trader_prefix=True,
    use_trader_id=True,
    streams_prefix="stream",
    buffer_interval_ms=None,     # write-through; Codex #3
)
```

With `stream_per_topic = False`, Nautilus publishes **all** message bus events for a given `trader_id` to a **single** Redis Stream:

```
trader-MSAI-{deployment_slug}-stream
```

Each entry on the stream carries the original topic (`events.order.filled`, `events.position.opened`, `events.account.state`, etc.) as a field inside the message so the projection consumer (3.4) can route by topic after `XREADGROUP`.

**Why not `stream_per_topic = True`:** That mode produces one stream per (topic, strategy) — e.g. `trader-{id}-stream-events.order.{strategy_id}`. The stream names are only known after the strategy is loaded, which means FastAPI can't subscribe at deployment start time. Redis has no wildcard `XREADGROUP`, so the consumer would have to poll for new stream names — a worse contract than knowing the single stream name up front. v3 chooses the single-stream mode and has the translator dispatch on the in-message topic field.

**Stream name is registered at deployment start:** Task 1.14 (`/api/v1/live/start`) computes `stream_name = f"trader-MSAI-{deployment_slug}-stream"` from the deterministic identities and writes it to the `live_deployments` row (new column `message_bus_stream`). The projection consumer (3.4) reads this column when it joins the consumer group — no guessing, no polling.

TDD: parallel to 3.1. Add a test that asserts `stream_per_topic is False` on the live config and `message_bus_stream` on a fresh deployment row matches `f"trader-MSAI-{slug}-stream"`.

Effort: S
Depends on: 1.5
Gotchas: #7, #8, #17, Codex #3, #4, Codex v2 P1 (stream discoverability)

---

#### 3.3 — Internal event schema (stable frontend contract)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/__init__.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/events.py` (new)
- `claude-version/backend/tests/unit/test_projection_events.py` (new)

Pydantic models for the internal MSAI schema (stable, decoupled from Nautilus):

- `PositionSnapshot { deployment_id, instrument_id, qty, avg_price, unrealized_pnl, realized_pnl, ts }`
- `FillEvent { deployment_id, client_order_id, instrument_id, side, qty, price, commission, ts }`
- `OrderStatusChange { deployment_id, client_order_id, status, reason, ts }`
- `AccountStateUpdate { deployment_id, account_id, balance, margin_used, margin_available, ts }`
- `RiskHaltEvent { deployment_id, reason, set_at }`
- `DeploymentStatusEvent { deployment_id, status, ts }`

TDD: serialization round-trip per model.

Effort: S
Depends on: nothing
Gotchas: Codex projection-layer recommendation

---

#### 3.4 — Redis Streams consumer + dual pub/sub (events + state) + DLQ

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/consumer.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/translator.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/fanout.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/state_applier.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/projection_state.py` (new)
- `claude-version/backend/src/msai/services/nautilus/projection/registry.py` (new)
- `claude-version/backend/tests/integration/test_projection_consumer.py` (new)
- `claude-version/backend/tests/integration/test_projection_fanout.py` (new)
- `claude-version/backend/tests/integration/test_projection_state_applier.py` (new)
- `claude-version/backend/tests/integration/test_projection_dlq.py` (new)

There are TWO separate background tasks per uvicorn worker (v5 split):

**Task 1: ProjectionConsumer (one logical consumer, but each uvicorn worker runs one).**

Reads the Nautilus message bus stream via consumer groups and publishes to BOTH pub/sub channels:

1. On startup, queries `live_deployments` for all rows with an active `live_node_processes` row (status in ready/running) and pulls their `message_bus_stream` name (from 3.2)
2. Joins the Redis consumer group `msai-projection` on each active stream via `XGROUP CREATE MKSTREAM` (idempotent). Each worker uses a UNIQUE consumer name within the group (e.g. `f"projection-{hostname}-{worker_pid}"`) so each entry is delivered to exactly one worker.
3. **PEL recovery via XAUTOCLAIM** for entries idle longer than `min_idle_ms` (default 30s) — Codex v3 P0
4. **PEL DLQ** (Codex v4 P2): entries reaching `max_delivery_attempts=5` are XADDed to `msai:live:events:dlq:{deployment_id}` with the original payload preserved + `dlq_reason`, then XACKed on the primary stream
5. Consumes via `XREADGROUP BLOCK 5000 COUNT 100`
6. Decodes Nautilus events using `MsgSpecSerializer` from `nautilus_trader.serialization.serializer`
7. Routes by the in-message `topic` field — translator is a `dict[topic_prefix, translator_fn]` lookup
8. Translates each Nautilus event to the internal schema (3.3) via `translator.py`
9. **Publishes to TWO pub/sub channels** (decision #4 in v5 changes, fixes Codex v4 P1):
   - `msai:live:state:{deployment_id}` — state-update channel that EVERY uvicorn worker subscribes to and applies to its OWN ProjectionState (Task 2)
   - `msai:live:events:{deployment_id}` — WebSocket fan-out channel that the WebSocket handlers subscribe to and forward verbatim to clients
10. `XACK`s the Redis stream message ONLY after both PUBLISHes succeed (at-least-once delivery, ACK only on success)
11. Every `recovery_interval_s` (default 30s), re-runs `XAUTOCLAIM` to catch peers crashing in steady state
12. On deployment start, a new stream is registered via `StreamRegistry` — picked up on the next loop iteration

**Task 2: StateApplier (one per uvicorn worker).**

Subscribes to ALL `msai:live:state:*` pub/sub channels via Redis pattern subscription (`PSUBSCRIBE msai:live:state:*`) and applies the deserialized event to this worker's local `ProjectionState`. This is the v5 fix for Codex v4 P1 — every uvicorn worker now sees every state update regardless of which worker's consumer pulled it from the stream.

```python
class StateApplier:
    """Background task per uvicorn worker that subscribes to the
    state-update pub/sub channels and feeds events into THIS worker's
    ProjectionState.

    Why a separate task from ProjectionConsumer:
    - The consumer reads from the message bus STREAM (consumer group;
      each entry processed by exactly one worker).
    - The state applier reads from the state PUB/SUB CHANNEL (pattern
      subscription; every worker receives every event).
    - Together they make ProjectionState consistent across workers
      while still using the consumer-group's exactly-once delivery
      semantics for the stream.
    """

    def __init__(self, redis: Redis, projection_state: ProjectionState) -> None:
        self._redis = redis
        self._state = projection_state

    async def run(self, stop_event: asyncio.Event) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe("msai:live:state:*")
        try:
            while not stop_event.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    continue
                channel = msg["channel"].decode()
                deployment_id = UUID(channel.rsplit(":", 1)[-1])
                event = InternalEvent.model_validate_json(msg["data"])
                self._state.apply(deployment_id, event)
        finally:
            await pubsub.punsubscribe("msai:live:state:*")
            await pubsub.close()
```

**Wiring at FastAPI startup (lifespan):**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = await get_redis()
    projection_state = ProjectionState()
    app.state.projection_state = projection_state
    app.state.position_reader = PositionReader(projection_state, REDIS_HOST, REDIS_PORT)

    stop_event = asyncio.Event()
    state_applier = StateApplier(redis, projection_state)
    state_task = asyncio.create_task(state_applier.run(stop_event))

    consumer = ProjectionConsumer(redis, ...)  # only some workers will actually pull
    consumer_task = asyncio.create_task(consumer.run(stop_event))

    yield

    stop_event.set()
    await asyncio.gather(state_task, consumer_task, return_exceptions=True)
```

**`projection_state.py`** — in-memory live state, fed by two sources: (a) the `StateApplier` task (from the pub/sub channel, for subsequent events) and (b) `PositionReader` cold-reads (hydrating the state with whatever the ephemeral Cache returns on the first query for a deployment).

```python
class ProjectionState:
    """In-memory rolling state of every active deployment.

    Per-uvicorn-worker instance. Two write paths:

    1. StateApplier: translated events from msai:live:state:* pub/sub
       (PositionSnapshot, AccountStateUpdate, PositionClosed, etc.)
    2. PositionReader cold path: hydrates the state with the result of
       Cache.cache_all() on first query for a deployment the worker
       has never observed.

    Used by PositionReader (3.5) as the fast path for snapshot reads.

    v7 changes from v6 (Codex v6 P1):

    - DROPPED the `_seen` flag. v6's `has_seen` was too coarse in both
      directions:
      - A FillEvent or OrderStatusChange marked the deployment seen but
        did not touch positions, so the fast path returned [] even when
        real positions existed in Redis.
      - A stream entry that didn't get through to apply() (e.g. filtered
        before dispatch) never flipped the flag, so the cold path fired
        on every request for that deployment.

    - INSTEAD: PositionReader.get_* always checks ProjectionState first.
      If the state has a map entry for the deployment (even an empty
      dict), that IS the answer. If there's no map entry at all, the
      cold path runs and writes its result back into ProjectionState
      via hydrate_from_cache_read(). After that, subsequent reads hit
      the fast path naturally.

    - Empty-but-hydrated deployments are represented by an explicit
      empty dict in self._positions (not a missing key). The distinction
      between "no key" (cold) and "empty dict" (hydrated, zero
      positions) is what makes the single hydration point work.
    """

    def __init__(self) -> None:
        # Key presence is the hydration signal. Missing key → cold.
        # Present key with empty dict → hydrated, zero positions.
        self._positions: dict[UUID, dict[str, PositionSnapshot]] = {}
        self._accounts: dict[UUID, AccountStateUpdate | None] = {}

    def apply(self, deployment_id: UUID, event: InternalEvent) -> None:
        """Event-driven write path. Called by StateApplier (3.4)
        from the msai:live:state:* pub/sub channel."""
        match event:
            case PositionSnapshot():
                self._upsert_position(deployment_id, event)
            case OrderStatusChange():
                pass  # not state-relevant; intentionally does NOT hydrate
            case FillEvent():
                pass  # the follow-up PositionSnapshot covers it
            case AccountStateUpdate():
                self._accounts[deployment_id] = event
            case PositionClosedEvent():
                self._remove_position(deployment_id, event.instrument_id)

    def hydrate_from_cold_read(
        self,
        deployment_id: UUID,
        *,
        positions: list[PositionSnapshot] | None = None,
        account: AccountStateUpdate | None = None,
    ) -> None:
        """Cold-read write path.

        v8 change (Codex v7 P1): ONLY-IF-STILL-COLD semantics.
        v7 blindly merged cold-read positions into existing state,
        which could overwrite a fresher `PositionClosed` that
        StateApplier applied between the cold read and this call.
        v8 skips the write for any domain that was hydrated between
        the caller starting the cold read and this call.

        The caller MUST check `is_positions_hydrated` / `is_account_hydrated`
        BEFORE starting the cold read (PositionReader does this in
        get_open_positions / get_account). If the check is False at
        call time and still False here, we hydrate. If another write
        path (StateApplier) raced in between, we drop the cold data
        on the floor — the newer event wins.

        The caller then returns the CURRENT state value, not the
        cold-read result, so even in the race case the caller sees
        the freshest data.

        Called with positions=[] marks positions as hydrated with
        zero content. Subsequent fast-path reads return [] without
        touching Redis.
        """
        if positions is not None and deployment_id not in self._positions:
            # Only-if-still-cold: another writer has not populated
            # self._positions[deployment_id] yet. Safe to hydrate.
            snapshot_map: dict[str, PositionSnapshot] = {}
            for snapshot in positions:
                snapshot_map[str(snapshot.instrument_id)] = snapshot
            self._positions[deployment_id] = snapshot_map
        if account is not None and deployment_id not in self._accounts:
            # Only-if-still-cold for the account domain too
            self._accounts[deployment_id] = account

    def is_positions_hydrated(self, deployment_id: UUID) -> bool:
        """True if positions for this deployment have been observed
        on this worker (via apply() or hydrate_from_cold_read). The
        presence of the key in self._positions is the hydration
        signal — empty dict counts as hydrated."""
        return deployment_id in self._positions

    def is_account_hydrated(self, deployment_id: UUID) -> bool:
        """True if the account for this deployment has been observed
        on this worker. Note: None is a valid hydrated value (deployment
        has no account state yet), so this must distinguish "key
        present" from "key absent"."""
        return deployment_id in self._accounts

    def positions(self, deployment_id: UUID) -> list[PositionSnapshot]:
        return list(self._positions.get(deployment_id, {}).values())

    def account(self, deployment_id: UUID) -> AccountStateUpdate | None:
        return self._accounts.get(deployment_id)
```

**`registry.py`** — `StreamRegistry` (unchanged from v4 except docstring):

```python
class StreamRegistry:
    """Tracks which streams the projection consumer should be reading.

    Every worker maintains its own view. On change, the consumer
    re-reads live_deployments and updates the set of active streams.
    Uses Redis pub/sub channel "msai:live:stream-registry-changed"
    as a change notifier.
    """
    async def active_streams(self) -> dict[UUID, str]: ...
    async def notify_change(self) -> None: ...
```

**`fanout.py`** — publishes to BOTH channels in one helper:

```python
async def publish_event(
    redis: Redis,
    deployment_id: UUID,
    event: InternalEvent,
) -> None:
    """Publish a translated internal event to the deployment's two
    pub/sub channels:

    - msai:live:state:{deployment_id} — every uvicorn worker subscribes
      via StateApplier and applies to its own ProjectionState
    - msai:live:events:{deployment_id} — WebSocket handlers subscribe
      and forward verbatim to clients

    Both publishes must succeed before the projection consumer ACKs the
    underlying stream entry.
    """
    payload = event.model_dump_json()
    await redis.publish(f"msai:live:state:{deployment_id}", payload)
    await redis.publish(f"msai:live:events:{deployment_id}", payload)
```

**Why two channels not one:** Conceptually they carry the same payload, but the subscribers are different:

- The state channel is consumed by every uvicorn worker's `StateApplier` (background task, no per-client filtering)
- The events channel is consumed by the WebSocket handlers (per-client filtering by `deployment_id`, plus per-client connection lifecycles)
  Splitting them lets us tune each subscription independently and makes log queries unambiguous about which path delivered an update.

**Why Redis pub/sub not in-memory queues:** FastAPI runs with `--workers 2`. An in-memory queue lives inside a single uvicorn worker, so a WebSocket client connected to worker A only sees events from worker A's consumer (Codex v2 P1, then Codex v4 P1 for the corresponding `ProjectionState` issue). Redis pub/sub broadcasts to all subscribers.

**Pub/sub is non-durable — durability lives in the stream + consumer group.** Worker restart loses some pub/sub messages, but the next stream entry will land on whichever worker pulled it from the consumer group, and the recovery snapshot path (3.5 cold path via ephemeral Cache) covers stale state on cold workers.

**The translator is a pure function** `translate(nautilus_event_payload, topic: str) -> InternalEvent`. One mapper per Nautilus event type. Switch keyed by topic prefix (`events.order.*`, `events.position.*`, `events.account.*`).

**No TTL on positions** — Codex finding #5. Position snapshots live as long as the position is open. Cleaned up on `PositionClosed` events.

TDD:

1. Unit test translator with each Nautilus event type
2. Integration test: publish a synthetic `OrderFilled` payload to the Redis stream, verify the consumer translates, publishes to BOTH channels, ACKs
3. Integration test: subscribe one StateApplier to `msai:live:state:*`, publish via `fanout.publish_event`, verify `ProjectionState.apply` is called
4. **Multi-worker consistency test (regression for Codex v4 P1)**: spin up TWO ProjectionState + StateApplier pairs (simulating two uvicorn workers); the projection consumer publishes ONE event; assert BOTH `ProjectionState` instances see the state update
5. **Multi-worker WebSocket fan-out**: spin up two WebSocket subscribers, publish one event via `fanout`, verify both receive it on the events channel
6. **PEL recovery integration test**: publish a message, consume it WITHOUT ACKing (simulate crash before publish), restart the consumer with `min_idle_ms=0` — verify `XAUTOCLAIM` reclaims, the new consumer publishes, ACKs
7. **PEL DLQ test**: simulate a message that fails translation 5 times in a row; assert it lands in `msai:live:events:dlq:{deployment_id}` with `dlq_reason="translation_failed"` and is XACKed on the primary stream
8. **PEL recovery negative test**: publish + consume + ACK normally, restart, verify NOT redelivered
9. **ACK-on-success-only test**: stub one of the two `PUBLISH`es to raise, verify the message is NOT ACKed and is reclaimed
10. Integration test: stream registry change — add a new deployment, verify the consumer picks up the new stream
11. Implement

Acceptance: tests pass.

Effort: L
Depends on: 3.2, 3.3
Gotchas: Codex v3 P0 (PEL semantics, ACK only on success), Codex v4 P1 (multi-worker ProjectionState via state pub/sub), Codex v4 P2 (DLQ for poison events)

---

#### 3.5 — `PositionReader` from in-memory `ProjectionState` (primary) + ephemeral `Cache` (fallback)

Files:

- `claude-version/backend/src/msai/services/nautilus/projection/position_reader.py` (new)
- `claude-version/backend/tests/integration/test_position_reader.py` (new)

v3's `PositionReader` kept long-lived `Cache` instances and called `cache_all()` once. That is wrong: a Nautilus `Cache` does NOT subscribe to Redis updates — `cache_all()` is a one-shot batch load. Snapshot reads would drift after the first call (Codex v3 P1). The import path was also wrong (`nautilus_trader.common.config` should be `nautilus_trader.cache.config`). v4 fixed both, but v4's `CacheDatabaseAdapter` constructor call only passed `trader_id` and `config`, omitting the required `instance_id` and `serializer` (Codex v4 P1).

**Verified `CacheDatabaseAdapter.__init__` signature** (`nautilus_trader/cache/database.pyx:132-138`, version 1.223.0):

```python
def __init__(
    self,
    TraderId trader_id not None,
    UUID4 instance_id not None,
    Serializer serializer not None,
    config: CacheConfig | None = None,
) -> None:
```

All four arguments are positional. v6 constructs the serializer the same way Nautilus does internally (`system/kernel.py:309-319`):

```python
import uuid
import msgspec

from nautilus_trader.cache.cache import Cache
from nautilus_trader.cache.database import CacheDatabaseAdapter
from nautilus_trader.cache.config import CacheConfig  # NOT common.config — Codex v3 P1
from nautilus_trader.common.config import DatabaseConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.identifiers import AccountId, StrategyId, TraderId
from nautilus_trader.serialization.serializer import MsgSpecSerializer  # Codex v5 P1 — correct class


class PositionReader:
    """Snapshot reads of positions/accounts for the live UI.

    Read flow (v7, Codex v6 P1 fix):

    1. Check is_positions_hydrated(deployment_id) / is_account_hydrated.
       - True: the state has been written (by StateApplier or by a
         previous cold read). Return state.positions() / state.account()
         verbatim. An empty list / None result is authoritative.
       - False: the cold path runs.
    2. Cold path: build an ephemeral Cache, cache_all(), read, dispose.
       THEN call state.hydrate_from_cold_read(...) with the result.
       Subsequent calls for the same deployment see is_*_hydrated() == True
       and use the fast path naturally.

    The previous v6 design used a single `has_seen` flag, which was
    too coarse in both directions: (a) FillEvent and OrderStatusChange
    flipped the flag without updating positions, so the fast path
    returned [] even when real positions existed in Redis; (b) filtered
    events never flipped the flag, so the cold path fired forever. v7
    replaces that with per-domain hydration (is_positions_hydrated,
    is_account_hydrated) AND has the cold read populate the state so
    the fast path is warm after exactly one cold call.

    NEVER keeps a long-lived Cache. The Cache is a one-shot loader,
    not a live view (Codex v3 P1).
    """

    def __init__(
        self,
        projection_state: ProjectionState,
        redis_host: str,
        redis_port: int,
    ) -> None:
        self._state = projection_state
        self._cache_config = CacheConfig(
            database=DatabaseConfig(type="redis", host=redis_host, port=redis_port),
            encoding="msgpack",
        )

    async def get_open_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        # Fast path: per-domain hydration flag (v7 fix for Codex v6 P1)
        if self._state.is_positions_hydrated(deployment_id):
            return self._state.positions(deployment_id)
        # Cold path: read from Redis, then hydrate the state.
        positions = await self._read_via_ephemeral_cache_positions(
            deployment_id, trader_id, strategy_id_full
        )
        # v8 (Codex v7 P1): hydrate is only-if-still-cold. If the
        # StateApplier raced us between the fast-path check and the
        # cold read, the hydrate is a no-op and state.positions()
        # returns the fresher pub/sub data instead of our stale
        # cold-read result.
        self._state.hydrate_from_cold_read(deployment_id, positions=positions)
        # Return the CURRENT state value, not the cold-read result.
        # In the race case, this returns the fresher data.
        return self._state.positions(deployment_id)

    async def get_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        if self._state.is_account_hydrated(deployment_id):
            return self._state.account(deployment_id)
        account = await self._read_via_ephemeral_cache_account(
            deployment_id, trader_id, account_id
        )
        self._state.hydrate_from_cold_read(deployment_id, account=account)
        # v8: return CURRENT state, not the cold-read result
        return self._state.account(deployment_id)

    def _build_adapter(self, trader_id: str) -> CacheDatabaseAdapter:
        """Construct a fresh CacheDatabaseAdapter with the verified
        Nautilus 1.223.0 signature.

        - trader_id: from the live_deployments row (deterministic
          MSAI-{slug}, decision #7)
        - instance_id: fresh UUID4 per request (adapter-instance ID,
          not trader ID)
        - serializer: MsgSpecSerializer constructed the same way
          nautilus_trader/system/kernel.py:313-317 constructs it —
          encoding is the msgspec MODULE, not a string; timestamps_as_str
          is True to match the subprocess (kernel.py:315 hardcoded)
        - config: the same CacheConfig the subprocess uses
        """
        return CacheDatabaseAdapter(
            trader_id=TraderId(trader_id),
            instance_id=UUID4(uuid.uuid4().hex),
            serializer=MsgSpecSerializer(
                encoding=msgspec.msgpack,  # module, NOT the string "msgpack"
                timestamps_as_str=True,
                timestamps_as_iso8601=False,
            ),
            config=self._cache_config,
        )

    async def _read_via_ephemeral_cache_positions(
        self,
        deployment_id: UUID,
        trader_id: str,
        strategy_id_full: str,
    ) -> list[PositionSnapshot]:
        """Build a fresh Cache, load, read, dispose. Per-request."""
        adapter = self._build_adapter(trader_id)
        try:
            cache = Cache(database=adapter)
            cache.cache_all()
            raw_positions = cache.positions_open(strategy_id=StrategyId(strategy_id_full))
            return [self._to_snapshot(p, deployment_id) for p in raw_positions]
        finally:
            adapter.close()  # release the Redis connection promptly

    async def _read_via_ephemeral_cache_account(
        self,
        deployment_id: UUID,
        trader_id: str,
        account_id: str,
    ) -> AccountStateUpdate | None:
        adapter = self._build_adapter(trader_id)
        try:
            cache = Cache(database=adapter)
            cache.cache_all()
            account = cache.account(AccountId(account_id))
            return self._to_account_update(account, deployment_id) if account else None
        finally:
            adapter.close()
```

**Why `MsgSpecSerializer(encoding=msgspec.msgpack, timestamps_as_str=True)`:** matches how Nautilus itself constructs the serializer at `system/kernel.py:313-317`. The `encoding` parameter is the msgspec **module** (`msgspec.msgpack` or `msgspec.json`), and the class uses `encoding.encode` / `encoding.decode` internally (`serialization/serializer.pyx:58-59`). Passing a string (as v5 did) or using a different class name (`MsgPackSerializer`, which doesn't exist) would fail at construction time. `timestamps_as_str=True` matches the subprocess's serializer config (kernel.py:315 `# Hardcoded for now`) so reads decode correctly.

TDD:

1. Unit test fast-path: pre-populate `ProjectionState` via `apply(PositionSnapshot(...))`, call `get_open_positions`, verify the position is returned without touching Redis (mock the Redis client and assert no calls)
2. **Fast-path empty-but-hydrated test (v7, regression for Codex v6 P1)**: call `state.hydrate_from_cold_read(deployment_id, positions=[])`, then call `get_open_positions` — assert the empty list is returned from the fast path WITHOUT touching Redis. Verify `is_positions_hydrated` returns True after the hydrate call.
3. **Non-state-changing event does NOT fake-hydrate (v7, regression for Codex v6 P1)**: call `state.apply(deployment_id, FillEvent(...))` ONLY. Call `get_open_positions` — assert the cold path IS taken (because `FillEvent` doesn't populate `_positions`), Redis IS queried, and the result is then written back via `hydrate_from_cold_read`.
4. **Cold path — never hydrated**: empty `ProjectionState`, no events, mock the ephemeral Cache to return one position, verify: (a) the position is returned, (b) the adapter is closed, (c) `is_positions_hydrated(deployment_id)` is True AFTER the call
5. **Cold path fires only once (v7)**: call `get_open_positions` for a cold deployment (cold path runs, hydrates state), then call it again with the Redis client mocked to raise — assert the second call succeeds without touching Redis, because the fast path sees the hydrated state.
6. **Per-domain hydration test (v7)**: call `state.hydrate_from_cold_read(deployment_id, positions=[...])` only. Assert `is_positions_hydrated` is True but `is_account_hydrated` is False. Calling `get_account` still triggers the cold path; calling `get_open_positions` does not.
7. **Constructor signature test (regression for Codex v4 P1)**: instantiate `PositionReader._build_adapter("MSAI-test")` with the real `CacheDatabaseAdapter` (not mocked), verify it does not raise `TypeError: missing required argument 'instance_id'`
8. **Serializer verification (regression for Codex v5 P1)**: verify `_build_adapter` constructs `MsgSpecSerializer(encoding=msgspec.msgpack, timestamps_as_str=True, timestamps_as_iso8601=False)` — assert `MsgPackSerializer` does NOT appear in the call path, and the `encoding` kwarg is the MODULE `msgspec.msgpack`, not the string `"msgpack"`
9. Integration test: start a minimal live subprocess writing to a testcontainers Redis with `CacheConfig.database = redis`, submit a synthetic order that opens a position, call `get_open_positions` from a fresh PositionReader, verify the position appears via the cold path AND subsequent calls use the fast path (Redis call count == 1 across multiple reads)
10. Integration test: feed an event through the projection consumer (PositionSnapshot), assert `is_positions_hydrated` is True, then call `get_open_positions` and assert the fast path serves it without touching Redis
11. Integration test: two deployments with distinct `trader_id`s — assert PositionReader correctly isolates them via the trader_id parameter
12. **Drift test (regression for Codex v3 P1)**: build a long-lived `Cache`, `cache_all()`, write a new position to Redis from a different process, re-read from the same long-lived `Cache` — assert it does NOT see the new position (proves the v3 design was wrong and the v4/v5/v6/v7 ephemeral pattern is necessary)
13. **Multi-worker fast-path test (regression for Codex v4 P1)**: spin up TWO ProjectionState instances (simulating two uvicorn workers), feed one event through the StateApplier on each, assert BOTH PositionReader instances see the position via fast path
14. Implement

Acceptance: tests pass.

Effort: M
Depends on: 3.1, 3.4 (ProjectionState + StateApplier definitions live there)
Gotchas: Codex v3 P1 (correct import path, ephemeral Cache, no long-lived Cache drift), Codex v4 P1 (CacheDatabaseAdapter signature requires instance_id + serializer), Codex v5 P1 (correct class is `MsgSpecSerializer` with the msgspec MODULE as encoding), Codex v6 P1 (per-domain hydration flags + cold-read hydrates state; no coarse `has_seen` flag)

---

#### 3.6 — WebSocket broadcaster via Redis pub/sub

Files:

- `claude-version/backend/src/msai/api/websocket.py` (full rewrite)
- `claude-version/backend/tests/integration/test_websocket_live_events.py` (new)

Replaces the heartbeat-only WebSocket. The handler:

1. Auths via first-message JWT/API-key (existing contract)
2. Requires a `deployment_id` path or query parameter
3. On connect, sends a snapshot: current positions and account state from `PositionReader` (3.5) using the `trader_id` and `account_id` looked up from the `live_deployments` row
4. Subscribes to the Redis pub/sub channel `msai:live:events:{deployment_id}` via `aioredis.client.PubSub.subscribe`
5. Forwards each received JSON message to the WebSocket verbatim (the projection consumer already produced the stable internal-schema JSON in 3.4)
6. Sends an application-level heartbeat every 30s if idle
7. On disconnect, unsubscribes from the pub/sub channel

```python
@router.websocket("/api/v1/live/stream/{deployment_id}")
async def live_stream(
    websocket: WebSocket,
    deployment_id: UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    position_reader: PositionReader = Depends(get_position_reader),
) -> None:
    await websocket.accept()
    # First message must be bearer/API-key — existing contract
    try:
        await _authenticate(websocket)
    except AuthError:
        await websocket.close(code=4401)
        return

    deployment = await db.get(LiveDeployment, deployment_id)
    if deployment is None:
        await websocket.close(code=4404)
        return

    # Send initial snapshot
    positions = await position_reader.get_open_positions(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        strategy_id=deployment.strategy_id_full,
    )
    account = await position_reader.get_account(
        deployment_id=deployment_id,
        trader_id=deployment.trader_id,
        account_id=deployment.account_id,
    )
    await websocket.send_json({"type": "snapshot", "positions": [p.model_dump() for p in positions], "account": account.model_dump() if account else None})

    # Subscribe to pub/sub fan-out
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"msai:live:events:{deployment_id}")
    heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket))

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            await websocket.send_text(message["data"].decode())
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        await pubsub.unsubscribe(f"msai:live:events:{deployment_id}")
        await pubsub.close()
```

**Multi-worker correctness:** Every uvicorn worker can serve this WebSocket because every worker subscribes to the same pub/sub channel. When the projection consumer (3.4) publishes an event, Redis delivers it to every subscribed worker, each of which forwards to its own connected clients. No in-memory state is shared across workers.

**Heartbeat is an application-level JSON `{"type": "heartbeat", "ts": ...}`**, not a TCP keepalive. Clients use it to detect dead sockets.

TDD:

1. Integration test: connect, expect snapshot with empty positions
2. Publish an event via `fanout.publish_event`, verify the WebSocket client receives it
3. Integration test: two WebSocket clients connected (simulate two uvicorn workers), publish one event, verify **both** receive it exactly once
4. Integration test: disconnect, verify pub/sub subscription is cleaned up
5. Implement

Effort: M
Depends on: 3.4, 3.5
Gotchas: Codex v2 P1 — pub/sub fan-out replaces in-memory queues

---

#### 3.7 — `RiskAwareStrategy` mixin (replaces custom RiskEngine subclass)

Files:

- `claude-version/backend/src/msai/services/nautilus/risk/risk_aware_strategy.py` (new)
- `claude-version/backend/tests/unit/test_risk_aware_strategy.py` (new)

Per the natives audit and Codex finding #2: the Nautilus `LiveRiskEngine` cannot be subclassed via config. We use a Strategy mixin instead.

**Portfolio API — correct method names per Codex v3 P1:**

The Nautilus `Portfolio` API has both per-instrument and per-venue accessors with **different names**:

| Scope                                  | PnL                                      | Exposure                                    | Returns                              |
| -------------------------------------- | ---------------------------------------- | ------------------------------------------- | ------------------------------------ |
| Per-instrument                         | `total_pnl(instrument_id)`               | `net_exposure(instrument_id)`               | `Money \| None`                      |
| Per-venue (aggregated, multi-currency) | `total_pnls(venue)` (plural)             | `net_exposures(venue)` (plural)             | `dict[Currency, Money]`              |
| Per-venue, per-currency                | `total_pnls(venue, target_currency=USD)` | `net_exposures(venue, target_currency=USD)` | `dict[Currency, Money]` (single key) |

v3 wrongly called `portfolio.total_pnl(venue)` (singular form, expects InstrumentId, not Venue). v4 uses the correct plurals for venue-level aggregates.

**Halt flag is no longer the primary kill switch (decision #16):**

v3 had the strategy poll `msai:risk:halt` on every `on_bar` and refuse new orders if set. That allowed up to one bar of lag on an emergency halt. v4 makes the kill switch push-based (decision #16 / task 3.9): the supervisor SIGTERMs every running deployment immediately on `/kill-all`, and Nautilus's `manage_stop=True` flattens automatically. The halt flag in the strategy mixin remains as **defense in depth** — a third layer to refuse new orders if for some reason the supervisor's stop didn't reach the subprocess (e.g., a network blip in the command bus). The lag is acceptable because it's the third layer, not the primary.

```python
from decimal import Decimal
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order


class RiskAwareStrategy(AuditedStrategy):
    """Strategy mixin that runs custom pre-submit risk checks BEFORE
    calling submit_order.

    Checks (in order):
    1. Sticky kill switch (Redis key msai:risk:halt, cached) — defense
       in depth; primary kill switch is push-based (3.9, decision #16)
    2. Per-strategy max position (per-instrument net position vs limit)
    3. Daily loss limit (per-venue total PnL via portfolio.total_pnls)
    4. Max notional exposure (per-venue net exposure via portfolio.net_exposures)
    5. Market hours (via MarketHoursService reading instrument_cache.trading_hours from Phase 2)

    On any failure: log a structured warning, write a "denied" row to
    order_attempt_audits, do NOT submit. Strategies use this by calling
    self.submit_order_with_risk_check(order) instead of self.submit_order.

    Built-in Nautilus checks (precision, native max_notional_per_order,
    rate limits) still run because we configure LiveRiskEngineConfig
    in 3.8 — this mixin is in addition to those, not instead.

    Uses the Portfolio API (self.portfolio.*), not direct Cache reads,
    because Portfolio is the stable Strategy-side abstraction for
    PnL and exposure aggregation.
    """

    def submit_order_with_risk_check(self, order: Order) -> None:
        venue = Venue(order.instrument_id.venue.value)
        instrument_id = order.instrument_id

        # 1. Kill switch (defense in depth — primary is 3.9)
        if self._halt_flag_cached:
            self._audit.write_denied(order, reason="risk:halt")
            return

        # 2. Per-strategy max position (per-instrument)
        position_for_instrument = self.portfolio.net_position(instrument_id)
        if not self._within_position_limit(order, position_for_instrument):
            self._audit.write_denied(order, reason="risk:position_limit")
            return

        # 3. Daily loss limit via portfolio.total_pnls(venue) — PLURAL
        # Returns dict[Currency, Money]; we sum across currencies after
        # converting to USD via target_currency.
        venue_pnls = self.portfolio.total_pnls(venue, target_currency=USD)
        if venue_pnls and not self._within_daily_loss_limit(venue_pnls):
            self._audit.write_denied(order, reason="risk:daily_loss")
            return

        # 4. Max notional exposure via portfolio.net_exposures(venue) — PLURAL
        venue_exposures = self.portfolio.net_exposures(venue, target_currency=USD)
        if venue_exposures and not self._within_exposure_limit(venue_exposures, order):
            self._audit.write_denied(order, reason="risk:exposure")
            return

        # 5. Market hours (Phase 4 task 4.3 provides MarketHoursService)
        if not self._within_market_hours(order):
            self._audit.write_denied(order, reason="risk:market_hours")
            return

        self.submit_order_with_audit(order)

    def _within_daily_loss_limit(self, pnls: dict[Currency, Money]) -> bool:
        """Sum the per-currency PnLs (already converted to USD by Nautilus
        because we passed target_currency=USD). Compare against the
        configured daily loss limit on the deployment.
        """
        total = sum(money.as_double() for money in pnls.values())
        return total > -float(self._risk_limits.daily_loss_limit_usd)

    def _within_exposure_limit(self, exposures: dict[Currency, Money], order: Order) -> bool:
        """Sum the venue-level net exposures (USD-converted) and add the
        order's notional. Reject if the projected total exceeds the limit.
        """
        current_total = sum(money.as_double() for money in exposures.values())
        order_notional = float(order.quantity) * float(order.price or 0)
        projected = current_total + order_notional
        return projected <= float(self._risk_limits.max_notional_exposure_usd)

    async def _refresh_halt_flag(self) -> None:
        """Called from on_bar via async task. Reads msai:risk:halt.

        Defense in depth — the primary kill switch is the supervisor
        SIGTERM in 3.9. This cached read just refuses any new orders the
        strategy might emit between the SIGTERM being sent and the
        subprocess actually exiting.
        """
        self._halt_flag_cached = bool(await self._redis.get("msai:risk:halt"))
```

**Why per-venue plurals not singulars:** `Portfolio.total_pnl(instrument_id)` is a per-instrument query. `Portfolio.total_pnls(venue)` (plural) returns the venue-level aggregate as `dict[Currency, Money]`. The same naming applies to `net_exposure(instrument_id)` vs `net_exposures(venue)`. v3 mixed them up (Codex v3 P1).

TDD:

1. Unit test each check in isolation with a mock `self.portfolio`
2. **Per-venue API test (regression for Codex v3 P1)**: assert the mixin calls `portfolio.total_pnls(venue, target_currency=USD)` (plural) NOT `portfolio.total_pnl(venue)` (singular)
3. **Per-instrument API test**: assert `net_position(instrument_id)` (not `net_position(venue)`)
4. **Multi-currency aggregation test**: feed a `dict[Currency, Money]` with USD and EUR, verify `_within_daily_loss_limit` sums them via `as_double()`
5. Test that orders pass through when within limits
6. Test that orders are denied when over limits, with the right `reason` on the audit row
7. Test that halt-flag refresh is called from `on_bar` before the risk check
8. Implement

Effort: L
Depends on: 1.11, 1.2 (audit table)
Gotchas: Codex v3 P1 (Portfolio API plurals vs singulars), decision #16 (halt flag is defense in depth, not primary)

---

#### 3.8 — Configure built-in `LiveRiskEngineConfig` with real limits

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/tests/unit/test_live_node_config_risk.py` (new)

Populate Nautilus's built-in risk engine with native throttles:

```python
risk_engine=LiveRiskEngineConfig(
    bypass=False,
    max_order_submit_rate="100/00:00:01",  # 100 per second
    max_order_modify_rate="100/00:00:01",
    max_notional_per_order={
        # Populated from RiskLimits on the deployment row
        "AAPL.NASDAQ": Decimal("100000"),
        # ...
    },
    debug=False,
)
```

The custom checks (per-strategy max position, daily loss, kill switch, market hours) are NOT here — they're in the `RiskAwareStrategy` mixin from 3.7. Nautilus's built-in handles only what it natively supports.

TDD:

1. Test that the live config installs the right native limits
2. Test that backtest config does NOT install live limits (uses defaults)
3. Implement

Effort: S
Depends on: 1.5
Gotchas: Codex #2

---

#### 3.9 — Push-based kill switch (supervisor SIGTERM + persistent halt flag)

Files:

- `claude-version/backend/src/msai/services/risk_engine.py` (extend existing)
- `claude-version/backend/src/msai/api/live.py` (modify `/kill-all`, add `/resume`)
- `claude-version/backend/tests/integration/test_kill_switch.py` (new)

v3 had the strategy poll the halt flag on every `on_bar` — up to one bar of lag (Codex v3 P2). v4 made the kill switch a push, but the halt flag was only re-checked at the HTTP `/start` entry — any `start` command already queued in `msai:live:commands` (or later reclaimed from the PEL) would still launch (Codex v4 P0). v5 adds a fourth layer: the supervisor itself re-checks the halt flag inside `ProcessManager.spawn` as the LAST step before `process.start()` (decision #16, implemented in 1.7 phase B).

**Layer 1 — Persistent halt flag (prevents NEW starts at the API):**

`POST /api/v1/live/kill-all` first sets `msai:risk:halt = true` in Redis with a long TTL (24h). The `/api/v1/live/start` endpoint reads this flag at the very top and returns 503 immediately if set. This blocks any new deployment from being launched at the API.

**Layer 2 — Supervisor-side halt-flag check (NEW IN v5, blocks queued/PEL starts):**

`ProcessManager.spawn` (1.7 phase B) re-checks `msai:risk:halt` AFTER reserving the DB slot but BEFORE `process.start()`. If set, it updates the row to `status='failed'` with `error_message='blocked by halt flag'` and returns True (the supervisor still ACKs the command — it was successfully handled, the failure is recorded; no retry needed until the operator clears the halt). This catches:

- Commands already in `msai:live:commands` when `/kill-all` was issued
- Commands later reclaimed from the PEL via `XAUTOCLAIM` (e.g., a previously crashed supervisor's pending entries)
- Race conditions between the kill-all RPC and a concurrent `/start` request (the supervisor wins the race because it always re-checks)

This is the v5 fix for Codex v4 P0.

**Layer 3 — Supervisor push (immediate flatten of running deployments):**

For every `live_node_processes` row with `status IN ('starting','building','ready','running')`, the kill-all endpoint publishes a `stop` command via `LiveCommandBus.publish_stop(deployment_id, reason="kill_switch")`. The supervisor processes these commands the same way it processes a normal `/stop`: SIGTERM the subprocess, escalate to SIGKILL after 30s. Because the strategy is built with `manage_stop = True` (decision #11), Nautilus's native exit loop cancels orders and flattens positions automatically when the strategy receives the stop signal.

Latency from operator click to flatten: roughly the `XADD` round-trip + the supervisor's `XREADGROUP BLOCK 5000` window + SIGTERM delivery + Nautilus stop. Realistically < 5 seconds. This is the **primary** mechanism for already-running deployments.

**Layer 4 — Strategy in-process halt (defense in depth):**

The `RiskAwareStrategy` mixin (3.7) caches the halt flag and refuses new orders if set. This is the fourth layer — it catches any orders the strategy might emit between the supervisor sending SIGTERM and Nautilus actually processing it. The lag is acceptable because it's the fourth layer, not the primary.

```python
@router.post("/kill-all", status_code=200)
async def kill_all(
    db: AsyncSession = Depends(get_db),
    bus: LiveCommandBus = Depends(get_command_bus),
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
) -> KillAllResponse:
    # Layer 1: persistent halt flag
    await redis.set("msai:risk:halt", "true", ex=86400)
    await redis.set("msai:risk:halt:set_by", user.id.hex, ex=86400)
    await redis.set("msai:risk:halt:set_at", utcnow().isoformat(), ex=86400)

    # Layer 2: push stop commands to every active deployment
    rows = await db.execute(
        select(LiveNodeProcess).where(
            LiveNodeProcess.status.in_(("starting", "building", "ready", "running"))
        )
    )
    halted = 0
    for row in rows.scalars():
        await bus.publish_stop(row.deployment_id, reason="kill_switch")
        halted += 1

    logger.warning("kill_switch_triggered", deployments=halted, set_by=str(user.id))
    return KillAllResponse(deployments_halted=halted)


@router.post("/resume", status_code=200)
async def resume(
    redis: Redis = Depends(get_redis),
    user: User = Depends(get_current_user),
) -> ResumeResponse:
    """Clear the halt flag. Required before /start will accept new deployments.
    No auto-resume — operator must explicitly unblock."""
    await redis.delete("msai:risk:halt")
    logger.warning("kill_switch_resumed", resumed_by=str(user.id))
    return ResumeResponse(resumed=True)
```

`/api/v1/live/start` is updated (small change to 1.14) to read the halt flag at the very top:

```python
@router.post("/start", ...)
async def start_live_deployment(...):
    if await redis.exists("msai:risk:halt"):
        raise HTTPException(status_code=503, detail="Kill switch is active. POST /api/v1/live/resume to clear.")
    # ... rest of /start ...
```

TDD:

1. Integration test: start two stub subprocesses, call `/kill-all`, verify two stop commands published to the bus, verify halt flag set
2. Integration test: with halt flag set, call `/start`, verify 503 returned with the right detail (Layer 1)
3. Integration test: call `/resume`, verify halt flag cleared and `/start` accepts new deployments
4. **Layer 2 test (regression for Codex v4 P0)**: pre-publish a `start` command to `msai:live:commands` (simulating a queued command); set the halt flag; start the supervisor; verify the supervisor consumes the command, ProcessManager.spawn re-checks the halt flag, marks the row `status='failed'` with `error_message='blocked by halt flag'`, ACKs the command, and does NOT spawn a child process
5. **Layer 2 test (PEL reclaim variant)**: publish a start command, consume it WITHOUT ACKing (simulate previous-supervisor crash), set the halt flag, restart the supervisor — verify XAUTOCLAIM reclaims the entry, the spawn is blocked by the halt flag, the row is marked failed
6. Integration test (real subprocess): start one trading subprocess via the supervisor with the smoke strategy, call `/kill-all`, verify the subprocess exits with status="stopped", verify position count is zero (Layer 3 — manage_stop flatten worked)
7. **Latency test (Layer 3)**: from `/kill-all` POST to subprocess `status="stopping"` row update, assert < 5 seconds
8. **Layer 4 defense-in-depth test**: bypass the supervisor (don't publish stop commands), instead set the halt flag directly and call `RiskAwareStrategy.submit_order_with_risk_check` from a unit test — verify the order is denied with reason="risk:halt"
9. Implement

Effort: M
Depends on: 3.7, 1.6, 1.7 (the supervisor-side check is implemented in ProcessManager.spawn phase B)
Gotchas: decision #16 (push not poll), Codex v4 P0 (supervisor-side halt-flag check inside spawn — not just at the API), #13 (manage_stop handles flatten)

---

#### 3.10 — Frontend live page wired to real WebSocket events

Files:

- `claude-version/frontend/src/app/live-trading/page.tsx` (modify)
- `claude-version/frontend/src/components/live/positions-table.tsx` (modify)
- `claude-version/frontend/src/components/live/strategy-status.tsx` (modify)
- `claude-version/frontend/src/lib/use-live-stream.ts` (new hook)

Replace mock data with `useLiveStream(deploymentId)`. Vitest unit test for the hook with a mock WebSocket. Visual test against a running deployment (manual).

Effort: L
Depends on: 3.6

---

#### 3.11 — Phase 3 E2E

Files: `claude-version/backend/tests/e2e/test_live_streaming_phase3.py` (new)

1. Start the stack with paper IB Gateway
2. Deploy a strategy
3. Connect to `/api/v1/live/stream` WebSocket
4. Receive snapshot
5. Trigger a fill (via the smoke strategy from 1.15)
6. Verify the WebSocket receives the translated `FillEvent` within 5 seconds
7. Verify `PositionReader` returns the new position
8. POST `/api/v1/live/kill-all`
9. Verify both positions closed and the halt flag is set
10. POST `/api/v1/live/start` again — should fail due to halt
11. POST `/api/v1/live/resume`, then start succeeds

Effort: L
Depends on: 3.1–3.10

---

### Phase 3 task ordering

```
3.1, 3.2, 3.3 (parallel — config + schema only, no code-level conflicts)
  ↓
3.4 (depends on 3.2, 3.3)
  ↓
3.5 (depends on 3.1)
  ↓
3.6 (depends on 3.4, 3.5)
  ↓
3.7 (depends on 1.11, can start any time after Phase 1)
  ↓
3.8 (depends on 1.5, can start any time after Phase 1)
  ↓
3.9 (depends on 3.7)
  ↓
3.10 (depends on 3.6)
  ↓
3.11 (depends on all)
```

---

## Phase 4 — Recovery + Reconnect + Market Hours + Metrics

**Goal:** Production-grade resilience. Mostly enabling Nautilus's built-in features and testing them.

**Phase 4 acceptance (revised per Codex v4):**

- `LiveExecEngineConfig.reconciliation = True` runs at startup; the subprocess writes `status="ready"` only after `wait_until_ready()` (decision #14, simplified in v5) confirms `kernel.trader.is_running` (the canonical FSM-RUNNING signal that only trips after `_trader.start()` succeeds at the end of `start_async`). The diagnose helper for failure messages uses the real Nautilus accessors (`data_engine.check_connected()` method, `exec_engine.check_connected()` method, per-`LiveExecutionClient.reconciliation_active`, `portfolio.initialized`, `cache.instruments()` count).
- `NautilusKernelConfig.load_state = True` and `save_state = True` are enabled; `EMACrossStrategy.on_save` and `on_load` are implemented and validated by a unit round-trip test
- A two-leg `BacktestNode` restart-continuity test against testcontainers Redis verifies: leg 2 loads state written by leg 1, the EMA values are continuous, the next bar does NOT generate a duplicate decision (decision #7 stable trader_id is what makes the cache keys collide across legs)
- Killing the FastAPI container does NOT interrupt trading (already true from Phase 1, re-tested in scenario A; the projection consumer reconnects and reclaims via XAUTOCLAIM)
- Killing the trading subprocess is detected by the supervisor's `reap_loop` within seconds (decision #15) and the row is flipped to `failed` with the real exit_code; if the supervisor is also dead, the heartbeat monitor flips it on the next supervisor restart
- IB Gateway disconnect for >2 minutes halts the strategy; on reconnect the strategy stays paused until manual `/resume`
- Equity strategies auto-pause outside RTH (using `instrument_cache.trading_hours` from Phase 2)
- Prometheus metrics exposed at `/metrics`
- **Scenario D dropped:** the live-feed restart assertion was non-deterministic. The deterministic equivalent is the two-leg `BacktestNode` integration test in 4.5, which exercises the exact same Nautilus save_state/load_state path with a Redis backend identical to production.

**The acceptance is NOT "strategy resumes" unconditionally** (Codex #10 correction): strategies either resume from validated state OR remain paused until operator manually warms them.

### Phase 4 tasks

#### 4.1 — Enable reconciliation + state persistence in live node config

Files:

- `claude-version/backend/src/msai/services/nautilus/live_node_config.py` (modify)
- `claude-version/backend/tests/unit/test_live_node_config_recovery.py` (new)

```python
exec_engine=LiveExecEngineConfig(
    reconciliation=True,
    reconciliation_lookback_mins=1440,
    inflight_check_interval_ms=2000,
    inflight_check_threshold_ms=5000,
    position_check_interval_secs=60,
)

# In TradingNodeConfig:
load_state=True,  # gotcha — defaults to False; Codex #10
save_state=True,  # same
```

The trading subprocess in 1.8 writes `status="ready"` only after `wait_until_ready(node)` (decision #14) confirms is_running, both engines connected, reconciliation complete, and at least one instrument loaded. v3 wrongly assumed `kernel.start_async()` returning was equivalent to "reconciliation completed" — it isn't (Codex v3 P0). v4 verifies explicitly via the post-start health check.

TDD:

1. Live config has `load_state=True`, `save_state=True`, `reconciliation=True`
2. Backtest config has all three at default False
3. Implement

Effort: S
Depends on: 1.5
Gotchas: Codex #10, #11

---

#### 4.2 — IB disconnect handler with halt-on-extended-disconnect

Files:

- `claude-version/backend/src/msai/services/nautilus/disconnect_handler.py` (new)
- `claude-version/backend/tests/integration/test_disconnect_handler.py` (new)

Background task in the trading subprocess:

1. Subscribes to Nautilus's connection state events
2. On disconnect: starts a timer
3. If reconnect within `disconnect_grace_seconds` (default 120s): no action, log only
4. If grace expires: set local halt flag (via Redis kill switch from 3.9 with `reason="ib_disconnect"`), trigger flatten via `Strategy.on_stop`'s logic
5. Stays halted until manual `/resume` (consistent with Codex's "remain paused until warm" wording)

TDD:

1. Mock IB connection events: simulate disconnect+quick-reconnect, verify no halt
2. Simulate disconnect+timeout, verify halt
3. Simulate halt + reconnect, verify no auto-resume
4. Implement

Effort: M
Depends on: 3.9
Gotchas: relates to #10

---

#### 4.3 — Market hours awareness via `instrument_cache.trading_hours`

Files:

- `claude-version/backend/src/msai/services/nautilus/market_hours.py` (new)
- `claude-version/backend/tests/unit/test_market_hours.py` (new)

```python
class MarketHoursService:
    """Reads trading_hours from instrument_cache (Phase 2 task 2.2 + 2.4)
    and exposes is_in_rth(canonical_id, ts) -> bool.

    Used by RiskAwareStrategy._within_market_hours.
    """

    async def is_in_rth(self, canonical_id: str, ts: datetime) -> bool: ...
    async def is_in_eth(self, canonical_id: str, ts: datetime) -> bool: ...
```

Per-strategy `allow_eth: bool = False` config. If False (default), orders outside RTH are denied.

TDD:

1. AAPL at 10am ET (in RTH, true), at 3am ET (out, false)
2. ESM5 at 10am ET (in, futures trade ETH)
3. allow_eth=True bypasses
4. Implement

Effort: M
Depends on: 2.2, 2.4 (Phase 2 must populate trading_hours)
Gotchas: Codex #9

---

#### 4.4 — Orphaned-process detection (supervisor-side, heartbeat-based)

Files:

- `claude-version/backend/src/msai/live_supervisor/heartbeat_monitor.py` (extend — already introduced in 1.7)
- `claude-version/backend/src/msai/main.py` (lifespan — recovery discovery, NO PID probing)
- `claude-version/backend/src/msai/services/nautilus/recovery.py` (new — recovery discovery helper)
- `claude-version/backend/tests/integration/test_heartbeat_orphan_detection.py` (new)
- `claude-version/backend/tests/integration/test_recovery_on_startup.py` (new)

v2 proposed `os.kill(pid, 0)` from FastAPI to detect orphaned subprocesses. That doesn't work — FastAPI and the trading subprocess live in different container namespaces, so their PIDs are meaningless to each other (Codex v2 P0). v3 makes **heartbeat freshness** the sole liveness signal:

**Supervisor side (extend HeartbeatMonitor from 1.7):**

```python
class HeartbeatMonitor:
    """Runs inside the live-supervisor container.

    v9 authority split (Codex v6 P0): HeartbeatMonitor scans
    POST-STARTUP statuses only — ready / running / stopping. The
    Watchdog (ProcessManager.watchdog_loop) is the sole liveness
    authority during starting / building. See decision #17 for the
    full rationale. If a prior revision of this file said this
    scanned 'starting'/'building' rows, that is v6 behavior that
    was removed in v7.

    Every 10 seconds:
    1. Selects live_node_processes rows with status in ('ready','running','stopping')
    2. For each row where last_heartbeat_at < now() - stale_seconds (default 30s):
       - Updates row: status='failed', error_message='heartbeat timeout',
         failure_kind='unknown' (v9, Codex v8 P2 — post-startup stale
         means the subprocess died without reporting why)
       - Fires the AlertService with deployment_id, last_heartbeat_at, duration_stale
    3. Sleeps 10 seconds
    """
    async def _mark_stale_as_failed(self) -> None: ...
```

This is the **post-startup** orphan detector. It runs in the same container as the subprocess's parent (the supervisor spawned it via `mp.get_context("spawn").Process`), so even if the subprocess OS-died, the row's heartbeat will stop advancing and the monitor will flip it to `failed` within 30–40 seconds. **Startup statuses are the Watchdog's exclusive domain** — this monitor must never touch them.

**FastAPI side (recovery discovery only):**

On FastAPI lifespan startup, FastAPI does NOT probe PIDs. It only:

1. Queries `live_node_processes` for rows with `status in ("ready", "running")` and `last_heartbeat_at > now() - stale_seconds`
2. For each, **registers** the deployment with the projection consumer so the consumer re-joins the Redis consumer group for that deployment's stream (3.4)
3. Logs "discovered N surviving deployments after API restart"

That's it. If a row is stale, the supervisor's heartbeat monitor will have already flipped it to `failed` — FastAPI trusts the row state.

```python
# claude-version/backend/src/msai/services/nautilus/recovery.py
async def discover_surviving_deployments(
    db: AsyncSession,
    stale_seconds: int = 30,
) -> list[LiveDeployment]:
    """Return live_deployments that are likely still running.

    Heartbeat-based only — never PID-probes across container namespaces.
    The supervisor is the sole authority on process liveness.
    """
    stmt = (
        select(LiveDeployment)
        .join(LiveNodeProcess, LiveNodeProcess.deployment_id == LiveDeployment.id)
        .where(
            LiveNodeProcess.status.in_(("ready", "running")),
            LiveNodeProcess.last_heartbeat_at > utcnow() - timedelta(seconds=stale_seconds),
        )
    )
    return (await db.execute(stmt)).scalars().all()
```

**Cache rehydration, reconciliation, and state persistence are all automatic via Nautilus config from 4.1.** The only recovery code this task adds is the heartbeat monitor (already scaffolded in 1.7) and the FastAPI-side "re-register the projection consumer" helper.

TDD:

1. Unit test `HeartbeatMonitor._mark_stale_as_failed` with a mocked clock — verify rows older than `stale_seconds` flip to `failed`, fresher rows do not
2. Integration test: insert a `live_node_processes` row with `last_heartbeat_at = now() - 60s`, run the monitor iteration once, verify the row is `status="failed"`
3. Integration test: insert a row with `last_heartbeat_at = now() - 5s`, verify the monitor leaves it alone
4. Integration test: start FastAPI with one running row (fresh heartbeat), verify `discover_surviving_deployments` returns it and the projection consumer re-registers
5. Integration test: start FastAPI with one stale row, verify `discover_surviving_deployments` does NOT return it (the supervisor owns the flip-to-failed)
6. Verify FastAPI never calls `os.kill` in recovery code (grep test in CI)
7. Implement

Effort: M
Depends on: 1.1, 1.7, 1.8
Gotchas: Codex v2 P0 (no PID probing across container namespaces)

---

#### 4.5 — Strategy state persistence + restart-continuity test (via BacktestNode twice)

Files:

- `claude-version/strategies/example/ema_cross.py` (modify)
- `claude-version/backend/tests/integration/test_ema_cross_save_load_roundtrip.py` (new)
- `claude-version/backend/tests/integration/test_ema_cross_restart_continuity.py` (new)

Implement `on_save` and `on_load` on `EMACrossStrategy`:

```python
def on_save(self) -> dict[str, bytes]:
    """Persist EMA indicator state. Called by Nautilus kernel on shutdown
    when save_state=True.
    """
    return {
        "fast_ema_value": str(self.fast_ema.value).encode(),
        "slow_ema_value": str(self.slow_ema.value).encode(),
        "last_position_state": str(self._last_position_state).encode(),
        "last_decision_bar_ts": str(self._last_decision_bar_ts_ns or 0).encode(),
        "version": b"1",
    }

def on_load(self, state: dict[str, bytes]) -> None:
    """Restore EMA indicator state. Called by Nautilus kernel on startup
    when load_state=True.
    """
    if not state or state.get("version") != b"1":
        return  # Cold start
    self.fast_ema.update_raw(float(state["fast_ema_value"].decode()))
    self.slow_ema.update_raw(float(state["slow_ema_value"].decode()))
    self._last_position_state = state["last_position_state"].decode()
    self._last_decision_bar_ts_ns = int(state["last_decision_bar_ts"].decode()) or None
```

**Idempotency key (`last_decision_bar_ts`):** Nautilus replays any un-processed bars from its cache on restart. To prevent a duplicate decision on the first bar after restart, the strategy records the `ts_event` of the last bar that produced a trade decision. On restart, `on_bar` checks `bar.ts_event > self._last_decision_bar_ts_ns` before acting. This is the pattern that makes restart-continuity achievable without operator intervention.

**Why BacktestNode twice, not a live subprocess restart:** v2 proposed to restart a live TradingNode subprocess and feed it the next bar. That requires a deterministic bar feeder we don't have — IB Gateway's live feed is not reproducible. `BacktestNode` gives us deterministic, reproducible bar feeding AND full Nautilus kernel lifecycle (including `on_save`/`on_load`). It's the correct test vehicle.

**Why testcontainers Redis (NOT an "on-disk KV store"):** v3 invented an on-disk KV-store `StateSerializer` that does not exist in this Nautilus install — `nautilus_trader.common.config.DatabaseConfig` only supports `type="redis"` here (Codex v3 P1). v4 brings up a testcontainers Redis and points both BacktestNode runs at it via `CacheConfig.database = redis`. Nautilus writes the strategy state via its native cache-backed save_state path, the same path the live system uses.

The **restart-continuity test**:

```python
@pytest.fixture(scope="module")
def redis_container() -> Iterator[Redis]:
    """Spin up an isolated Redis for this test only."""
    from testcontainers.redis import RedisContainer
    with RedisContainer("redis:7-alpine") as container:
        yield container


def test_ema_cross_restart_continuity(redis_container) -> None:
    """Two-leg test against testcontainers Redis:

    Leg 1: Run BacktestNode on a N-bar catalog that triggers an EMA cross.
           save_state=True writes strategy state to the Redis-backed cache
           via Nautilus's native CacheDatabaseAdapter.

    Leg 2: New BacktestNode with the SAME trader_id and the SAME
           CacheConfig pointing at the SAME Redis. load_state=True reads
           the prior state on startup. Feed bar (N+1) only. Assert:
           (a) EMA fast/slow at the start of leg 2 == end of leg 1 (continuity)
           (b) No duplicate order on bar N+1 (idempotency via last_decision_bar_ts)
           (c) A subsequent "signal" bar still emits a new decision (not frozen)
    """
    redis_host = redis_container.get_container_host_ip()
    redis_port = redis_container.get_exposed_port(6379)
    cache_config = CacheConfig(
        database=DatabaseConfig(type="redis", host=redis_host, port=int(redis_port)),
        encoding="msgpack",
    )

    catalog = build_deterministic_catalog(n_bars=120, ema_cross_at_bar=60)
    common_kwargs = dict(
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        catalog_path=catalog,
        cache_config=cache_config,
        trader_id="MSAI-restart-test-0001",  # stable across both legs (decision #7)
    )

    # Leg 1: bars 0..99, crossing at bar 60, expect ≥1 decision; save_state=True
    result_a = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=False,
        save_state=True,
        bars_range=(0, 100),
    )
    assert len(result_a.orders) >= 1, "expected at least one order in leg 1"

    # Verify state was actually written to Redis (sanity)
    redis_client = Redis(host=redis_host, port=int(redis_port))
    state_keys = redis_client.keys(f"trader-MSAI-restart-test-0001:*")
    assert len(state_keys) > 0, "leg 1 should have written state to Redis"

    # Leg 2: bar 100 ONLY, load_state=True, expect NO duplicate
    result_b = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=True,
        save_state=True,
        bars_range=(100, 101),
    )
    assert len(result_b.orders) == 0, "leg 2 bar should not emit a duplicate"

    # Continuity check: leg 2's pre-bar EMA state matches leg 1's final state
    assert result_b.initial_fast_ema == pytest.approx(result_a.final_fast_ema, rel=1e-9)
    assert result_b.initial_slow_ema == pytest.approx(result_a.final_slow_ema, rel=1e-9)

    # Freshness check: a bar that WOULD trigger a new decision still does
    result_c = run_backtest(
        **common_kwargs,
        strategy_config={"order_id_tag": "restart-test-0001"},
        load_state=True,
        save_state=True,
        catalog_path=build_deterministic_catalog(n_bars=121, ema_cross_at_bar=120),
        bars_range=(100, 121),
    )
    assert len(result_c.orders) >= 1
```

`run_backtest` is a test helper that wraps `BacktestNode` with the kernel's `load_state`/`save_state` flags and the Redis `CacheConfig`. The same trader_id across both legs is what makes the cache keys collide and the state reload — enforced by decision #7 (stable deployment_slug).

**Why this is a test of the production path, not a test fixture artifact:** the live system also uses `CacheConfig.database = redis` (3.1) and `load_state=True`/`save_state=True` (4.1) with the same trader_id pattern. The integration test exercises the exact same code path Nautilus runs in production — only the data feed differs (deterministic bars instead of IB live).

**Separate round-trip test** (simpler, faster):

```python
def test_ema_cross_on_save_on_load_roundtrip() -> None:
    """Pure unit test: construct an EMA strategy, populate it, call
    on_save, construct a fresh instance, call on_load, assert state
    is restored."""
    strat = EMACrossStrategy(config=...)
    strat.fast_ema.update_raw(100.5)
    strat.slow_ema.update_raw(99.2)
    strat._last_position_state = "LONG"
    state = strat.on_save()

    fresh = EMACrossStrategy(config=...)
    fresh.on_load(state)
    assert fresh.fast_ema.value == pytest.approx(100.5)
    assert fresh.slow_ema.value == pytest.approx(99.2)
    assert fresh._last_position_state == "LONG"
```

TDD:

1. Round-trip unit test (above)
2. Two-leg BacktestNode restart-continuity integration test against testcontainers Redis (above)
3. Implement

Effort: M
Depends on: 3.1 (Redis CacheConfig), 4.1, 1.8
Gotchas: #16, Codex v2 P1 (BacktestNode twice, not live subprocess restart), Codex v3 P1 (testcontainers Redis, not on-disk KV-store), decision #7 (stable trader_id across legs)

---

#### 4.6 — Prometheus metrics

Files:

- `claude-version/backend/src/msai/services/observability/metrics.py` (new)
- `claude-version/backend/src/msai/main.py` (mount `/metrics`)
- `claude-version/backend/tests/integration/test_metrics_endpoint.py` (new)

`prometheus_client`-based registry:

- Counters: `msai_orders_submitted_total`, `msai_orders_filled_total`, `msai_orders_rejected_total`, `msai_orders_denied_total`, `msai_deployments_started_total`, `msai_deployments_failed_total`, `msai_kill_switch_triggered_total`
- Gauges: `msai_active_deployments`, `msai_position_count{deployment_id}`, `msai_daily_pnl_usd{deployment_id}`, `msai_unrealized_pnl_usd{deployment_id}`, `msai_ib_connected{deployment_id}`
- Histograms: `msai_order_submit_to_fill_ms`, `msai_reconciliation_duration_seconds`

The trading subprocess writes counter increments to a Redis key pattern; the FastAPI projection consumer reads them and exposes them via `/metrics`. Pure Nautilus events (no custom subprocess metric exporter required).

TDD:

1. `/metrics` returns Prometheus format
2. Metrics non-zero after a synthetic event
3. Implement

Effort: M
Depends on: 3.4

---

#### 4.7 — Phase 4 E2E (three scenarios)

Files: `claude-version/backend/tests/e2e/test_recovery_phase4.py` (new)

**Scenario A: Kill FastAPI mid-trade**

1. Deploy strategy
2. Wait for `status="running"`
3. `docker kill msai-claude-backend`
4. Sleep 5s
5. `docker compose up -d backend`
6. Verify trading subprocess still running (heartbeat advancing — supervisor's reap loop and heartbeat monitor are both unaffected by the FastAPI restart)
7. Verify `GET /api/v1/live/status/{deployment_id}` discovers it from the database
8. Verify the projection consumer joins the consumer group, runs `XAUTOCLAIM` to recover any pending entries from the previous instance's PEL, and resumes publishing to pub/sub

**Scenario B: Kill TradingNode subprocess**

1. Deploy strategy
2. SIGKILL the trading subprocess pid directly
3. Within 1–2 seconds: verify the supervisor's `reap_loop` (decision #15) flips the row to `status="failed"` with the real exit_code (-9 for SIGKILL)
4. Verify an alert was emitted
5. **(Recovery path)** SIGKILL the supervisor as well, then `docker compose up -d live-supervisor` — verify the supervisor's heartbeat monitor flips any other lingering rows to `failed` based on stale heartbeat (decision #15 fallback path)

**Scenario C: Disconnect IB Gateway**

1. Deploy strategy
2. `docker pause msai-claude-ib-gateway`
3. Wait 130 seconds (past `disconnect_grace_seconds`)
4. Verify the strategy halted (orders cancelled, positions closed via `manage_stop=True`)
5. `docker unpause msai-claude-ib-gateway`
6. Verify the strategy stays halted (manual resume required)
7. POST `/api/v1/live/resume`
8. Verify the strategy is restartable

**Scenario D — DROPPED in v4.** Codex v3 P1 flagged that the original Scenario D (restart a live deployment, feed it the next live bar, assert no duplicate) is non-deterministic against IB Gateway: the next live bar's contents and timing are uncontrollable, and flake risk is high. The same contract is now proven by the deterministic two-leg `BacktestNode` integration test in 4.5, which exercises the exact same Nautilus save_state/load_state path with a Redis backend identical to production (testcontainers Redis). The E2E loses no production-relevant coverage.

Effort: L
Depends on: 4.1–4.6

---

## Phase 5 — Paper Soak Release Gate (NOT implementation)

**Documentation only.** Exists in the plan so "Phase 4 done" cannot be misread as "ready for real money."

### 5.1 — Paper soak procedure

Document at `claude-version/docs/paper-soak-procedure.md`:

- **Duration:** 30 calendar days minimum
- **Account:** IB paper account, separate from real
- **Strategies:** start with one (EMA Cross on AAPL+MSFT), add one new instrument per week if no incidents
- **Monitoring:** daily PnL email, Prometheus alerts on API down, subprocess down, IB disconnect >2 min, reconciliation failure, halt set, manual review of audit log every Friday
- **Incidents:** any P0/P1 incident restarts the 30-day clock
- **Exit:** 30 consecutive days zero P0/P1 incidents AND manual sign-off AND audit log review

### 5.2 — Release sign-off checklist

Document at `claude-version/docs/release-signoff-checklist.md`:

- [ ] 30-day paper soak completed without incident
- [ ] All Phase 1–4 E2E tests passing on the latest commit
- [ ] All unit + integration tests passing
- [ ] Architecture review re-run by Claude + Codex against the latest code, no P0/P1/P2 findings
- [ ] Disaster recovery runbook tested
- [ ] Operator confirms emergency contact for IB account
- [ ] Initial real-money allocation: max $1,000, hard cap in `LiveRiskEngineConfig.max_notional_per_order`

**No code commits in this phase.**

---

## Cross-Cutting Concerns

### Test Strategy

TDD per task. Test pyramid: unit (every function/class) + integration (DB, Redis, subprocess) + E2E (full stack at end of each phase, gated by `MSAI_E2E_IB_ENABLED=1`).

### Logging and Observability

Structured logging with `deployment_id`, `strategy_id`, `client_order_id` context starting in Phase 1.

### Database Migrations

Each task adds an Alembic migration. Migration tests in `tests/integration/`.

### Backwards Compatibility

Existing backtest pipeline keeps working at every phase boundary. Phase 2 includes a migration script for existing `*.SIM` catalogs.

### Parallelization Notes

- **Phase 1** is fully sequential (1.1 → 1.16) — Codex #13 was correct that the original "Group D parallelizable" claim was wrong
- **Phase 2** has the parallelization map under section 2 above
- **Phase 3** has the map under section 3 above
- **Phase 4** is mostly sequential

---

## Open Questions

1. **IB account credentials in Key Vault** — who provisions the paper IB account and where do credentials live?
2. **Redis cluster vs single instance** — single is fine for Phase 3 dev; production may want redundancy. Defer to Phase 6.
3. **Postgres connection pooling under multi-deployment load** — may need pgbouncer in production. Defer.
4. **Strategy state schema versioning** — `on_save` payload changes require version handling. Defer until we have a second strategy.
5. **Multi-currency PnL** — defer until we have a non-USD strategy.

## Risks

1. **Nautilus version drift** — pin exact version; run upgrade tests in a separate branch.
2. **IB Gateway flakiness** — paper soak in Phase 5 is the mitigation.
3. **Subprocess orchestration complexity** — narrow contract (DB rows + Redis), no IPC primitives.
4. **Catalog migration data loss** — idempotent + dry-run mode + test against a copy.
5. **Phase boundary slippage** — re-evaluate scope at each phase boundary.

---

## How To Use This Plan

- **Future Claude Code sessions**: pick the next pending task in the lowest pending phase. Read the architecture review, the Nautilus reference, the natives audit, and the relevant gotchas before implementing. Do not skip TDD.
- **Codex CLI working in parallel on `codex-version/`**: this plan is Claude-only. Codex CLI can use this as a template for codex-version's own plan.
- **The user**: each task is sized to fit a single working session.
- **Phase boundaries are checkpoints**: don't start Phase N+1 until Phase N's E2E passes.

---

**Plan version:** 9.0 — IMPLEMENTATION-READY
**Last updated:** 2026-04-07
**Approved by:** Plan review loop CLOSED after v9 sanity pass. Seven iterations with Codex converged the architectural direction; remaining marginal risk is in diminishing-returns territory and will be caught during Phase 1 implementation and Phase 5 paper soak.

## Revision history

- **v1.0** (2026-04-06): initial 5-phase plan after architecture review
- **v2.0** (2026-04-06): incorporates Codex review of v1 (1 P0 + 9 P1 + 3 P2 fixed) and Nautilus natives audit (deletes 6 reinventing tasks, simplifies Phase 4 dramatically)
- **v3.0** (2026-04-06): incorporates Codex re-review of v2 (2 P0 + 7 P1 fixed) covering container topology and process ownership.
  - Dedicated `live-supervisor` Docker service replaces arq-hosted supervision (arq on_startup deadlock)
  - Heartbeat-only liveness detection replaces cross-container PID probing
  - Deterministic `trader_id`/`order_id_tag` from `deployment_slug`; deployments are now stably identifiable across restarts
  - `stream_per_topic = False` — one deterministic Redis Stream per trader so FastAPI can subscribe at deployment start
  - Redis pub/sub per deployment for WebSocket fan-out (multi-uvicorn-worker correctness)
  - FastAPI uses Nautilus `Cache` Python API instead of raw Redis key reads
  - `StrategyConfig.manage_stop = True` replaces custom `on_stop` flatten code
  - Parity harness redesigned: determinism test + config round-trip + intent contract (no more "TradingNode against IB paper")
  - Restart-continuity test uses `BacktestNode` run twice (deterministic bar feed) instead of live subprocess restart
  - `RiskAwareStrategy` uses `self.portfolio.account()/total_pnl()/net_exposure()` (stable Strategy-side API) instead of raw cache reads
- **v4.0** (2026-04-07): incorporates Codex re-review of v3 (3 P0 + 5 P1 + 2 P2 fixed) covering Redis Streams semantics, idempotency, the readiness gate, and the identity/schema model.
  - **Stable `deployment_slug`** decoupled from `live_deployments.id`. The slug is created once and reused across restarts so Phase 4 state reload actually works. 16 hex chars (64 bits) instead of 8 (32 bits).
  - **Redis Streams PEL recovery** via explicit `XAUTOCLAIM`. v3 wrongly assumed un-ACKed messages are auto-redelivered. They aren't — they sit in the per-consumer PEL until claimed. Both `LiveCommandBus` and the projection consumer recover stale pending entries explicitly.
  - **Idempotency at three layers**: DB-level partial unique index on `live_node_processes(deployment_id) WHERE status IN active`, supervisor-level pre-spawn check + ACK-only-on-success, HTTP-level `Idempotency-Key` header with Redis cache.
  - **Post-start health check** before writing `status="ready"`. `kernel.start_async()` returning is NOT proof of readiness. v4 polls `is_running and data_engine.is_connected and exec_engine.is_connected and not reconciliation_active and instruments_loaded > 0` until timeout.
  - **`live_deployments` schema migration** (task 1.1b) adds `deployment_slug`, `trader_id`, `strategy_id_full`, `account_id`, `message_bus_stream`, `instruments_signature`, `last_started_at`, `last_stopped_at`. Logical de-duplication via unique index on `(started_by, strategy_id, paper_trading, instruments_signature)`.
  - **`live_node_processes.pid` is nullable**, `building` added to status enum (v3 inconsistency fixed).
  - **`PositionReader` rebuilt**: in-memory `ProjectionState` populated by the projection consumer is the fast path; ephemeral per-request `Cache` is the cold-start fallback. Long-lived `Cache` instances are gone (they don't subscribe to Redis updates and would drift). Import path corrected.
  - **`RiskAwareStrategy` portfolio API names corrected**: `total_pnls(venue)` and `net_exposures(venue)` (plural — venue-level aggregates returning `dict[Currency, Money]`), not `total_pnl(venue)` / `net_exposure(venue)` (singular — those expect `InstrumentId`).
  - **Restart-continuity test against testcontainers Redis** instead of an invented "on-disk KV-store StateSerializer" (which doesn't exist in this Nautilus install). Two `BacktestNode` runs share the same Redis-backed cache via the same trader_id.
  - **Phase 4 Scenario D dropped**: live-feed restart was non-deterministic. The deterministic two-leg `BacktestNode` integration test in 4.5 covers the same contract with no production-relevant coverage loss.
  - **Supervisor keeps `dict[deployment_id, mp.Process]` handle map** for instant in-process exit detection via a `reap_loop` background task. Heartbeat is now only the cross-restart recovery signal.
  - **Kill switch is push-based**, not bar-poll. `/kill-all` sets the persistent halt flag AND publishes stop commands to the supervisor for every running deployment AND keeps the strategy-side mixin halt check as defense in depth. Latency from operator click to flatten < 5 seconds.
- **v5.0** (2026-04-07): incorporates Codex re-review of v4 (3 P0 + 3 P1 + 2 P2 fixed). Author verified all corrections against the installed `nautilus_trader 1.223.0` source before writing v5.
  - **Broader identity tuple** for `deployment_slug` derivation (decision #7). v4's `(user, strategy, instruments, paper)` was too coarse; v5 uses `(user, strategy, code_hash, config_hash, account_id, paper, instruments)` hashed via canonical-JSON sha256 to produce `identity_signature`. Any field change → cold start. Same identity → warm restart.
  - **`live_deployments.identity_signature` UNIQUE column** added in 1.1b. The single source of identity truth; replaces v4's composite uniqueness.
  - **`ProcessManager.spawn` uses INSERT-commit → halt-check → spawn → UPDATE-commit pattern**, NOT a single transaction (Codex v4 P0). Three transactions, irreversible side effect outside any of them. Adds `'stopping'` to the active-states query (v4 documented but missed it). Heartbeat monitor times out a phase-C failure to recover.
  - **Supervisor-side halt-flag check inside `ProcessManager.spawn` phase B** (Codex v4 P0). Catches commands queued before `/kill-all` and PEL-reclaimed entries that the HTTP-only check missed. Kill switch is now four layers (was three).
  - **Multi-worker `ProjectionState` via dual pub/sub channels** (Codex v4 P1). New `StateApplier` background task per uvicorn worker subscribes to `msai:live:state:*` (pattern subscribe) and applies events to its own `ProjectionState`. The projection consumer publishes to BOTH `msai:live:state:{deployment_id}` AND `msai:live:events:{deployment_id}` per event so every worker's state stays consistent and WebSocket fan-out still works.
  - **Post-start health check simplified to `kernel.trader.is_running`** (Codex v4 P1). Verified against `nautilus_trader/system/kernel.py:1001-1037`: the trader's FSM transitions to RUNNING only on the LAST line of `start_async`, after every internal await succeeds. The made-up engine attributes from v4 (`data_engine.is_connected`, engine-level `reconciliation_active`) don't exist in 1.223.0. The diagnose helper uses the REAL accessors (`check_connected()` methods, per-`LiveExecutionClient.reconciliation_active`).
  - **Heartbeat thread starts BEFORE `node.build()`** (Codex v4 P1). HeartbeatMonitor stale-sweep query now includes `'building'` and `'starting'`. A hung build is detected within `min(build_timeout_s, stale_seconds)` (was: never).
  - **`node.build()` wrapped in `asyncio.wait_for(loop.run_in_executor(None, build), timeout=120)`** as belt-and-suspenders against C-side hangs that the heartbeat thread can't escape from.
  - **`CacheDatabaseAdapter` constructor signature corrected** to `(trader_id, instance_id, serializer, config)` per the verified Nautilus 1.223.0 signature at `cache/database.pyx:132-138`. v4 only passed `trader_id` and `config`. v5 also adds `MsgPackSerializer(timestamps_as_str=True)` to match the live subprocess's encoding.
  - **`Idempotency-Key` uses atomic SETNX in-flight reservation** (Codex v4 P2). v4's post-hoc cache let two concurrent retries both publish; v5 reserves the slot via `SET ... NX EX 60` BEFORE any work. Concurrent retries get 425 Too Early. Key is user-scoped (`{user_id}:{key_hash}`) — eliminates cross-principal leak.
  - **PEL DLQ + delivery-count cap** for both the command bus and the projection consumer (Codex v4 P2). Entries reaching `max_delivery_attempts=5` are XADDed to a DLQ stream and XACKed off the primary. Poison messages no longer bounce forever.
- **v6.0** (2026-04-07): incorporates Codex re-review of v5 (2 P0 + 2 P1 + 2 P2 + 1 P3 fixed). Codex confirmed the v5 architectural direction (`kernel.trader.is_running`, `check_connected()` methods, per-client `reconciliation_active`, `CacheDatabaseAdapter` 4-arg signature) but flagged tactical errors in the build timeout, phase-C recovery, serializer class/signature, idempotency TTL, ProjectionState cold-path gate, a heartbeat ordering contradiction, a nonexistent dict accessor, and the config hash input. The author verified `serialization/serializer.pyx:36-62`, `system/kernel.py:309-319`, `execution/engine.pyx:147,204-214,269-283`, `portfolio/portfolio.pyx:218`, and `data/engine.pyx:296` directly before writing v6.
  - **Supervisor-side build watchdog via `ProcessManager.watchdog_loop`** (Codex v5 P0). v5 used `asyncio.wait_for(loop.run_in_executor(None, build), timeout=120)` which only cancels the awaiter, not the executor thread — a wedged C-side IB build stays alive. v6 removes the wait_for wrapper; the watchdog runs in the supervisor and SIGKILLs any child whose row is still `starting`/`building` past `build_timeout_s + startup_health_timeout_s` (default 180s total). Process-level kill always works; thread-level cancellation doesn't.
  - **Subprocess self-writes its pid as the first DB action** (Codex v5 P0). v5's phase-C failure path left `pid=NULL`, which made `/stop` and `/kill-all` silently return success without signaling after a supervisor restart (handle map empty, `row.pid is None`). v6 has the subprocess `UPDATE live_node_processes SET pid=os.getpid()` immediately after connecting to Postgres. `stop()` now always has a real pid to signal.
  - **`MsgSpecSerializer` with the verified construction** (Codex v5 P1). v5 mixed up class names and passed a string to `encoding`. The real class is `MsgSpecSerializer` in `nautilus_trader/serialization/serializer.pyx:36`, the `encoding` parameter is a module (e.g. `msgspec.msgpack`), and Nautilus itself constructs it at `system/kernel.py:313-317`. v6 uses exactly that construction everywhere (decision #10 example + task 3.5 `PositionReader._build_adapter`).
  - **`diagnose()` uses the private `_clients` dict** for per-ExecutionClient inspection (Codex v5 P3). `exec_engine.registered_clients` returns `list[ClientId]` (verified at `execution/engine.pyx:204-214`), not a dict of client objects. The real client objects live in the private `_clients` attribute; accessing it from the diagnose helper is acceptable because diagnose runs in the same process that constructed the kernel.
  - **Idempotency reservation TTL extended to 300s** (covers `build_timeout_s + startup_health_timeout_s + api_poll_timeout_s`). **Transient responses are no longer cached**: 425 Too Early, 504 Gateway Timeout, and 503 "kill switch active" call `release()` (not `commit_terminal()`) so retries can re-attempt. Only 201 Created and 503 with a permanent failure reason are cached. v5's 60s TTL was shorter than the startup path, and v5 cached 504s for 24h — both contradictions fixed.
  - **`ProjectionState.has_seen(deployment_id)` fast-path gate** (Codex v5 P2). v5's `PositionReader.get_open_positions` used `if positions:` as the fast-path check — an idle deployment with zero open positions fell through to `cache.cache_all()` on every request. v6 gates on `has_seen(deployment_id)`: once any event has been applied for a deployment, subsequent reads come from in-memory even if the result is an empty list. Cold-path `cache_all()` fires only once per deployment per worker restart.
  - **Heartbeat ordering contradiction resolved** (Codex v5 P2). v5's task 1.9 docstring still said "Started after `node.build()`" — contradicted decision #17 and task 1.8 which correctly ordered it before build. v6 updates task 1.9 to cross-reference task 1.8 for the canonical ordering and adds an ordering test.
  - **`config_hash` hashes the Pydantic-validated config model** (Codex v5 P3 nit). v5 hashed the raw request dict; semantically-identical configs (e.g. Pydantic coerces `"5"` → `5`) would have produced different hashes. v6 `compute_config_hash(config: BaseModel | dict)` dumps the model via `model_dump(mode="json")` before hashing.
- **v7.0** (2026-04-07): incorporates Codex re-review of v6 (1 P0 + 3 P1 fixed). Every Nautilus-native claim from v6 was explicitly verified by Codex — the architectural direction is settled. The remaining issues were all in our own glue code.
  - **Single startup-liveness authority** (Codex v6 P0). v6 had the HeartbeatMonitor and the Watchdog both scanning `starting`/`building` rows, with the HeartbeatMonitor's 30s stale-sweep racing the Watchdog's 180s wall-clock deadline. A wedged build got marked `failed` by the HeartbeatMonitor at t+30s (freeing the partial unique index slot) while the real process was still alive and no longer in the `/stop` / watchdog filter — a retry could spawn a duplicate child. v7 gives the Watchdog **exclusive** ownership of `starting`/`building`: the HeartbeatMonitor stale-sweep scans only `ready`/`running`/`stopping`, and the Watchdog is the sole code path that marks a startup row as `failed` (and only AFTER it has SIGKILLed the pid, in the same transaction — no window where the row is out of the active set but the process is alive).
  - **Watchdog deadline is heartbeat-based, not wall-clock** (Codex v6 P1). v6 killed rows with `started_at < now() - 180s` regardless of heartbeat progress — would have falsely killed legitimate slow IB contract loading (30 options underlyings at 10-30s each; see `docs/nautilus-reference.md:482,513`). v7 kills when `last_heartbeat_at < now() - stale_seconds` (default 30s) — a subprocess whose heartbeat thread is still advancing the timestamp is considered making progress and left alone. A secondary hard wall-clock ceiling at `startup_hard_timeout_s = 600s` catches pathological degenerate-loop cases.
  - **Cold-read hydrates `ProjectionState`; `has_seen` is removed** (Codex v6 P1). v6's `has_seen` flag had two failure modes: (a) `FillEvent` and `OrderStatusChange` flipped the flag without populating positions, so `get_open_positions` returned `[]` from the fast path even when Redis had the real state; (b) filtered events never flipped the flag, so the cold path fired forever. v7 replaces the single flag with per-domain `is_positions_hydrated` / `is_account_hydrated` and has `PositionReader`'s cold path **write its result back into `ProjectionState`** via a new `hydrate_from_cold_read()` method. After the first cold read, subsequent reads naturally hit the fast path because the state has a non-missing key (even if empty).
  - **`EndpointOutcome` dataclass + `FailureKind` enum** (Codex v6 P1). v6's `commit_terminal` allowlisted `{201, 422}` but the workflow docstring told callers to call `commit_terminal(503, ...)` on permanent failure — the helper would throw. v6 also distinguished "permanent 503" from "transient 503" by parsing the detail string (fragile), and had a 200-vs-201 mismatch in the already-active branch. v7 introduces a structured `EndpointOutcome(status_code, response, cacheable, failure_kind)` with factory methods (`ready`, `already_active`, `halt_active`, `in_flight`, `api_poll_timeout`, `permanent_failure`, `body_mismatch`). The idempotency layer's `commit()` reads `outcome.cacheable` directly — no status-code allowlist, no string parsing. A new `failure_kind` column is added to `live_node_processes` so the endpoint can read the enum (not parse strings) to decide cacheability. The `already_active` branch returns 200 (correctly, not 201).
- **v8.0** (2026-04-07): incorporates Codex re-review of v7 (2 P0 + 2 P1 + 1 P2 fixed). Codex confirmed no new Nautilus-native issues — "v7 does not add a new Nautilus API dependency; the problems are all in supervisor/DB/Redis glue." v8 fixes five glue-code bugs.
  - **Lock-first atomic watchdog path** (Codex v7 P0). v7's watchdog was still `scan → SIGKILL → SELECT FOR UPDATE → UPDATE`, which had a race: between the scan and the SIGKILL, the subprocess could flip to `ready`/`stopping`, and the post-kill SELECT FOR UPDATE's `status IN ('starting','building')` filter would miss the row. v8 holds the row-level lock across the entire kill-and-update sequence: SELECT FOR UPDATE inside a single transaction, re-check status UNDER the lock, SIGKILL, UPDATE, COMMIT. No concurrent writer can interleave.
  - **Only the `Reserved` branch owns `commit` / `release`** (Codex v7 P0). v7's `body_mismatch` factory was `cacheable=True`, and the workflow let step N call `commit()` from any cacheable outcome. But `BodyMismatchReservation` means the caller does NOT own the reservation slot — calling `commit()` there would overwrite the original correct response with a 422. v8 makes `body_mismatch` `cacheable=False` AND restricts `commit()`/`release()` to the `Reserved` branch only. `InFlight`, `CachedOutcome`, and `BodyMismatchReservation` return their outcome directly without touching the store.
  - **`failure_kind` writers wired + safe parser** (Codex v7 P1). v7 added the `failure_kind` column to the schema and had `/start` read it to decide cacheability, but the supervisor's `_mark_failed` and the subprocess finally block never populated it. v8 makes `_mark_failed(row_id, reason, failure_kind: FailureKind)` take the enum as a required arg and wires every failure path (halt-flag block → `HALT_ACTIVE`; `process.start()` failure → `SPAWN_FAILED_PERMANENT`; watchdog kill → `BUILD_TIMEOUT`; subprocess `StartupHealthCheckFailed` → `RECONCILIATION_FAILED`; generic exceptions → `SPAWN_FAILED_PERMANENT`). Also adds `FailureKind.UNKNOWN` variant and `FailureKind.parse_or_unknown(db_string)` helper — the endpoint never crashes on NULL or stale values.
  - **Cold-read hydration is only-if-still-cold** (Codex v7 P1). v7's `hydrate_from_cold_read` blindly merged cold-read positions into existing state and overwrote account state. If StateApplier applied a newer event between the cold read and the hydrate, the older cold snapshot would overwrite fresher pub/sub data. v8 makes `hydrate_from_cold_read` a no-op for any domain that was hydrated between the caller's fast-path check and the hydrate call (the check is `deployment_id not in self._positions/_accounts` at hydrate time). `PositionReader` also returns the CURRENT state value (not the cold-read result) after the hydrate — in the race case, the caller sees the fresher data.
  - **`startup_hard_timeout_s` raised to 1800s + per-deployment override** (Codex v7 P2). v7's 600s hard ceiling was tighter than legitimate large-options-universe builds (`docs/nautilus-reference.md:482,513` documents 900s+ is possible). v8 raises the supervisor default to 1800s (30 min) and adds a nullable `startup_hard_timeout_s` column on `live_deployments` so operators can raise it per deployment for extra-large universes.
- **v9.0** (2026-04-07): FINAL sanity pass on Codex v8 review (1 P0 + 2 P1 + 1 P2). Codex confirmed no new Nautilus-native issues. Plan review loop CLOSED after v9 — implementation begins at Phase 1.
  - **Watchdog pid-fallback via `self._handles`** (Codex v8 P0). v8's `_watchdog_kill_one` skipped SIGKILL when `row.pid is None` but still flipped the row to `failed`. A phase-C failure that left `pid=NULL` with a live handle in `self._handles` would cause the child to survive with a terminal row — retry → duplicate. v9 sources the pid from `row.pid OR self._handles[deployment_id].pid`. If neither has a pid, the watchdog logs ERROR, fires `watchdog_no_pid` alert, and DOES NOT flip the row — leaves it for the next iteration.
  - **SKIP LOCKED + per-row asyncio.wait_for(5s)** (Codex v8 P1). v8's serial candidate loop could stall the whole watchdog pass on one locked row. v9 uses `with_for_update(skip_locked=True)` so contended rows are silently skipped until the next pass, and wraps each `_watchdog_kill_one` call in `asyncio.wait_for(timeout=5)` as a safety belt against Postgres-side hangs.
  - **Stale prose references pruned** (Codex v8 P1). Four places where older-revision text contradicted the v8 summary were updated: (a) idempotency TDD test 1 now asserts `body_mismatch` is `cacheable=False` + adds a reserved-only commit enforcement test, (b) Phase 4 recovery-section HeartbeatMonitor prose now correctly says it scans `ready/running/stopping` only (removed the stale "starting/ready/running" list), (c) watchdog test 9 uses the 1800s backstop, not 600s, (d) watchdog decision #17 prose and in-code comments aligned to v9 parameter names (`default_stale_seconds` / `default_startup_hard_timeout_s`).
  - **`failure_kind` wired in `_on_child_exit` + `_mark_stale_as_failed`** (Codex v8 P2). v8 added failure_kind to `_mark_failed` but missed these two paths. v9 updates `_on_child_exit` to write `NONE` on clean exit and `SPAWN_FAILED_PERMANENT` on non-zero exit (only if still NULL — doesn't clobber a more specific value the subprocess already wrote in its finally block), and `HeartbeatMonitor._mark_stale_as_failed` to write `UNKNOWN` (post-startup stale without a root cause).

  **Plan review loop closed.** Further iterations have reached diminishing returns. The remaining marginal risk will be caught during Phase 1 implementation (where the actual code is in front of a compiler and tests) and the Phase 5 paper soak is the release gate for production readiness.
