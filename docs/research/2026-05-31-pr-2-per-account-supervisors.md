# Research: PR 2 — Per-Account Supervisor Ownership Boundary

**Date:** 2026-05-31
**Feature:** Replace the single shared `ProcessManager.handles` ownership map with an account-scoped supervisor ownership boundary so one supervisor restart/crash can't take down the whole IB fleet; crashed accounts auto-restart + IB-reconcile (gated by the account halt latch); per-account supervisor health surfaces on `/live/status` + CLI.
**Researcher:** research-first agent

> **Design fork this brief informs (PRD §7):** per-account supervisor **PROCESSES**
> (one OS process per account, each owning only its account's TradingNode) **vs.** a
> thin **control-plane router** that does NOT parent node lifetimes (self-supervising
> nodes + DB/Redis ownership records, so a router restart doesn't kill/orphan nodes).
> Every "Design impact" below is written against BOTH shapes. Phase 3 picks one.

---

## Libraries Touched

| Library                           | Our Version                                                         | Latest Stable             | Breaking Changes Since Ours                                                                                                                                                                  | Source                                                                                     |
| --------------------------------- | ------------------------------------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| NautilusTrader                    | **1.223.0** (`backend/.venv/.../nautilus_trader-1.223.0.dist-info`) | **1.227.0** (2026-05-18)  | Multi-account reconciliation correctness fixes (1.226/1.227); `ExecutionEngine.register_client` now errors on duplicate venue route (1.226, Rust); config builder-pattern migrations (1.226) | [GitHub releases](https://github.com/nautechsystems/nautilus_trader/releases) (2026-05-31) |
| Python `multiprocessing` (stdlib) | 3.12 (`requires-python >=3.12,<3.14`)                               | 3.12 stdlib               | N/A (stdlib)                                                                                                                                                                                 | [CPython #111873](https://github.com/python/cpython/issues/111873) (2026-05-31)            |
| arq                               | 0.26.0+ (`pyproject.toml`)                                          | 0.28.0 (maintenance-only) | None relevant — supervisor does not use arq                                                                                                                                                  | [arq docs](https://arq-docs.helpmanual.io/) (2026-05-31)                                   |
| redis-py (Redis Streams)          | 5.2.0+ (`pyproject.toml`)                                           | 5.x                       | None relevant — per-account stream helper already exists                                                                                                                                     | code: `live_command_bus.py:82`                                                             |

---

## Per-Library Analysis

### NautilusTrader

**Versions:** ours = 1.223.0, latest = 1.227.0 (Beta; the project is permanently "Beta"-tagged — 1.227.0 is the current stable line).

**Breaking changes since ours (relevant subset, verified against [RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md)):**

- **1.226.0:** `ExecutionEngine.register_client` now errors when a venue is already routed to another client (Rust). Single-account-per-node topologies are unaffected; a hypothetical "multiple accounts in one node sharing a venue" topology would now hard-error.
- **1.226.0:** "multi-account execution support" / `Portfolio` account-scoped `net_exposure`, `net_exposures`, balance updates in multi-account mode (Rust).
- **1.226.0:** config constructors migrated to builder patterns for some Rust config types (`SimulatedVenueConfig` etc.) — **does not** touch `TradingNodeConfig` / `LiveExecEngineConfig` Python construction we use.
- **1.227.0:** `LiveExecEngineConfig` gained missing config values (#3841) — additive.
- **1.227.0 reconciliation correctness fixes (THE load-bearing finding):**
  - "Fixed live position reconciliation **conflating positions across accounts**" (#4029)
  - "Fixed live position reconciliation **retry/throttle leaking across accounts** on the same instrument"
  - "Fixed live position reconciliation **collapsing multi-account positions on the same instrument**" (Rust)

**Deprecations:** None relevant to this feature.

**Recommended pattern (TradingNode lifecycle — verified against installed venv source):**

- `live/node.py:272 build()` → `283 run()` / `338 run_async()` → `381 stop()` / `398 stop_async()` → `409 dispose()`. **`run()` is NOT a coroutine** and `run_async()` must be awaited from inside an existing loop (matches `nautilus.md` gotcha #18). `dispose()` (`node.py:409-473`) blocks up to `timeout_disconnection`, cancels the streaming task, calls `kernel.dispose()`, shuts the executor down (`wait=True, cancel_futures=True`), and stops/closes the event loop — **mandatory in a `finally` to avoid the Rust-logger + IB-socket leak** (gotcha #20). The subprocess already does this (`trading_node_subprocess.py`).
- **Reconciliation completion IS verifiable, and the codebase already gates on it correctly.** `kernel.start_async()` (`system/kernel.py:1001-1037`) awaits `_await_execution_reconciliation()` (`kernel.py:1330-1344`) → `exec_engine.reconcile_execution_state(timeout_secs=config.timeout_reconciliation)`. **`self._trader.start()` is the LAST line of `start_async` (`kernel.py:1037`) and is reached ONLY if reconciliation returned True.** So `kernel.trader.is_running` flipping `True` is a _sufficient_ signal that startup reconciliation completed. `startup_health.py:wait_until_ready()` already polls exactly this signal (cited to `kernel.py:1037` in its own docstring). Internally, `LiveExecutionEngine._startup_reconciliation_event` (`execution_engine.py:152`, set in a `finally` at `:1746`) is the lower-level primitive, but it is gated by the kernel — we do **not** need to touch it; the `is_running` poll is the contract.
  - `LiveExecEngineConfig` fields (`live/config.py:76-216`): `reconciliation: bool = True`, `reconciliation_lookback_mins`, `reconciliation_instrument_ids`, `reconciliation_startup_delay_secs: PositiveFloat = 10.0`. The 10s post-reconciliation delay means a restarted node's "ready" latency has a floor — relevant to US-2's auto-restart timing expectations.
- **One TradingNode per OS process is mandatory.** Web docs + `nautilus.md` architectural rule #1/#4: "Running multiple TradingNode instances concurrently in the same process is not supported due to global singleton state." This is the single most important constraint on the design fork (see Design impact).
- **Orphan-and-re-adopt of a running TradingNode by a different parent: NOT supported by Nautilus.** Nautilus has no parent-process-death detection and no "attach to an already-running node" API. A TradingNode is an in-process Python object bound to its own asyncio loop and Rust core; it cannot be serialized, handed to, or re-parented into a different Python interpreter. "Re-adoption" in our system means re-discovering the _DB row_ + _OS pid_ (which the OS reparents to PID 1 on supervisor death) — NOT re-acquiring the Python object. The existing `ProcessManager.stop()` already relies on this: it falls back to `row.pid` + `os.kill` when `self.handles` is empty after a restart (`process_manager.py:1209-1222`).

**Sources:**

1. [NautilusTrader Releases](https://github.com/nautechsystems/nautilus_trader/releases) — accessed 2026-05-31
2. [RELEASES.md (raw)](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md) — accessed 2026-05-31
3. [Live Trading concept doc](https://nautilustrader.io/docs/latest/concepts/live/) — accessed 2026-05-31
4. Installed venv source (ground truth): `backend/.venv/lib/python3.12/site-packages/nautilus_trader/live/node.py:272-494`, `system/kernel.py:1001-1037,1330-1344`, `live/execution_engine.py:152,1330-1344,1746`, `live/config.py:76-216`

**Design impact (BOTH shapes):**

- **Per-account PROCESSES shape:** Strongly _consistent_ with Nautilus's "one node per process" rule — it's already how we run (one subprocess per deployment). What PR 2 adds is a per-account _owner_ of those subprocesses. The Nautilus constraint does NOT block this shape and does not require N TradingNodes-in-one — each account still has its own subprocess; only the _owner_ multiplies.
- **Thin-ROUTER shape:** Also viable — the router would NOT own the Python node object (it can't, per the orphan finding), so it relies on the already-existing pid+DB re-discovery path. A router restart leaving subprocesses running is _exactly_ what happens today on supervisor restart (`main.py` docstring: "The supervisor does NOT send SIGTERM to running trading subprocesses on shutdown … re-discovers surviving children via heartbeat-fresh rows"). The router shape formalizes this existing behavior; the process shape adds a true per-account fault domain.
- **Version-delta caution (applies to BOTH shapes):** We are on 1.223.0; the **multi-account reconciliation isolation bugs were only fixed in 1.226/1.227**. If the chosen shape ever puts two accounts' positions on the **same instrument** under reconciliation in a shared context, 1.223.0 can conflate/collapse them. Our topology keeps each account in its **own subprocess → own kernel → own exec engine → own reconciliation scope**, so these bugs do **not** bite us _as long as we never collapse accounts into one node_. This is a concrete argument for the per-account-process fault domain over any "one node, many accounts" temptation, AND a flagged risk if a future PR considers a Nautilus upgrade or a single-node-multi-account topology.

**Test implication:**

- Add a unit/integration test asserting that a restarted account's node does NOT reach `running`/accept orders until `kernel.trader.is_running` is `True` (i.e. reconciliation gate passed) — reuse the `startup_health.wait_until_ready` path; assert `RECONCILIATION_FAILED` classification on a reconciliation timeout (US-2 AC: "Node does NOT accept orders; surfaced as a degraded/failed health state").
- Add a regression assertion that the per-account topology never shares an exec-engine/reconciliation scope across accounts (guards against accidentally reintroducing the 1.223.0 cross-account reconciliation bug). A test that two accounts holding the SAME symbol report independent positions through `/live/positions` covers the user-visible side.
- Do **not** write a test that depends on `_startup_reconciliation_event` directly (private, and the kernel gating via `is_running` is the stable contract). Standard coverage of `dispose()` in `finally` already exists; extend it to the restart path.

---

### Python `multiprocessing` / process supervision (stdlib)

**Versions:** Python 3.12 (pinned `>=3.12,<3.14`; Nautilus IB extras unavailable on 3.14). `multiprocessing` is stdlib — no external lib for supervision is in `pyproject.toml`; restart/crash-loop guarding is **hand-rolled** in `ProcessManager` today (the startup watchdog at `process_manager.py:928-1060`, the reap loop at `:835-922`, the heartbeat monitor in `heartbeat_monitor.py`).

**Key facts (verified against sources + codebase):**

- **Orphan reparenting:** when the parent (supervisor) dies, its child trading subprocesses are **reparented to PID 1** (the container init). They keep running, keep their open IB socket, and keep their file descriptors. Inside a Docker container PID 1 must be an init that reaps zombies (or `mp.Process` children become un-reaped zombies after they exit). **The current compose `live-supervisor` service does NOT set `init: true`** (`docker-compose.prod.yml:349-426`, `docker-compose.dev.yml:208-237`) — this is fine today because the supervisor is PID 1 of its own container AND it never intentionally outlives its children, but it becomes load-bearing for the thin-router shape (see Design impact).
- **`mp.Process` ownership semantics:** the project uses `mp.get_context("spawn")` (`process_manager.py:159`) — clean interpreter, child does NOT inherit the parent's asyncio loop/fds beyond what's pickled. `Process.is_alive()` / `Process.exitcode` are only meaningful **from the parent that spawned it** and only while that parent lives (the reap loop's documented assumption — `process_manager.py:850-857`). After a supervisor restart the handle map is empty; liveness must come from `os.kill(pid, 0)` / `/proc/<pid>` / the DB heartbeat — which is exactly the existing re-discovery design.
- **Parent-death detection (Linux):** `prctl(PR_SET_PDEATHSIG, SIGTERM)` is the canonical mechanism to make a child receive a signal when its parent thread dies. It is **per-child, set by the child after fork/spawn**, and has a well-known caveat: it fires on the death of the parent _thread_, not the process, and is cleared across `execve` unless re-armed. Python has no stdlib wrapper; it requires `ctypes` into `libc.prctl`. This is the only way to make a subprocess self-terminate when its per-account owner dies — relevant ONLY if the design wants "owner dies ⇒ its account's node dies too" (which would _violate_ the PRD goal — see Design impact).
- **Process groups / signal propagation:** `os.setsid()` (new session/process group) lets you signal a whole account's process subtree with `os.killpg`. SIGTERM to a process group reaches every member. This is the clean primitive for **per-account graceful shutdown** vs. **fleet shutdown**: per-account = signal that account's group; fleet `/kill-all` = signal all groups. Today everything is one flat set under one supervisor, so `/kill-all` is a loop over `self.handles` + the Redis halt latch (`process_manager.py` + `halt_keys.py`).
- **Crash-loop / bounded-restart guard:** no stdlib primitive; must be hand-rolled (max-attempts + backoff). The PRD US-2 AC explicitly requires this ("a max-restart-attempts ceiling with a clear terminal `SPAWN_FAILED_*` state"). `tenacity` (already a dep, `pyproject.toml:36`) provides retry/backoff decorators and could back the restart ceiling instead of a bespoke counter — but tenacity is request-scoped retry, not a long-lived "N restarts per rolling window then latch terminal" supervisor policy, so a small hand-rolled counter persisted on the DB row (or a Redis counter keyed by account) is the more honest fit. `FailureKind` already has terminal `SPAWN_FAILED_*` kinds (`services/live/failure_kind.py`, used throughout `process_manager.py`).

**Sources:**

1. [`PR_SET_PDEATHSIG(2const)` man page](https://man7.org/linux/man-pages/man2/pr_set_pdeathsig.2const.html) — accessed 2026-05-31
2. [CPython #111873 — ProcessPoolExecutor workers stay alive after parent killed](https://github.com/python/cpython/issues/111873) — accessed 2026-05-31
3. [Orphan process handling in Docker (containerd)](https://petermalmgren.com/orphan-children-handling-containerd/) — accessed 2026-05-31
4. Codebase: `process_manager.py:159,835-922,928-1060,1209-1222`, `main.py` module docstring, `heartbeat_monitor.py`

**Design impact (BOTH shapes):**

- **Per-account PROCESSES shape:** Each per-account owner becomes a parent of _only_ its account's trading subprocess(es). If an owner crashes, the OS reparents that account's subprocess to PID 1 (it keeps trading + keeps its IB socket) — **good**, it satisfies the "other accounts unaffected" goal as long as the owners are independent OS processes (separate compose services OR separate child processes under a thin parent). Re-adoption after owner restart = the existing pid+DB re-discovery path, scoped to one account. **Do NOT use `PR_SET_PDEATHSIG` to tie a node's life to its owner** — that would make an owner crash kill the account's node, the exact fleet-survival behavior the PRD forbids (US-1). If owners are separate **compose services**, the single-Azure-VM memory envelope (16 GB, already near ceiling with the broker fleet — PRD §5) must budget N supervisor processes; this is a capacity analysis for Phase 3, flagged here as a known constraint.
- **Thin-ROUTER shape:** The router never parents node lifetimes, so orphan-reparenting is the _normal_ steady state, not an exception. This makes **`init: true` (or a real init like tini) on the container that hosts the trading subprocesses load-bearing** — without it, a subprocess that exits while the router is restarting becomes an unreaped zombie, and the router's reap path (which relies on `Process.is_alive()` from the _original_ parent) is gone. The router must detect liveness via `os.kill(pid,0)`/`/proc`/heartbeat exclusively. This shape needs the bounded-restart guard to be **fully DB/Redis-backed** (no in-memory restart counter survives a router restart).
- **Both shapes need a per-account graceful-stop vs. fleet-stop signal story.** `os.setsid` + `killpg` gives a clean per-account-group SIGTERM; the fleet `/kill-all` stays a fan-out over accounts + the existing fleet halt latch. The account halt latch (`account_halt_key`, `halt_keys.py:33`) already exists and MUST gate any auto-restart (US-2 safety-critical AC + §6 "owner can NEVER override an operator's halt").

**Test implication:**

- Test that an auto-restart is **suppressed** when `account_halt_key(account_id)` is set (US-2: "An account with an active halt latch … is NOT auto-restarted"). The spawn path already re-checks this latch twice (`process_manager.py:248-303,523-576`); the auto-restart path must funnel through the same check, not bypass it.
- Test the bounded-restart ceiling: simulate N consecutive crashes of one account, assert it stops restarting after the ceiling and lands a terminal `SPAWN_FAILED_*` row visible on `/live/status` (US-2 edge case "Node crashes repeatedly … Bounded retries, then terminal failure state … no infinite loop").
- Test fleet isolation: with two accounts' subprocesses running, kill account A's owner and assert account B's pid is still alive (`os.kill(pid_b, 0)` succeeds) and its heartbeat row stays fresh — i.e. no shared in-memory structure took B down (US-1 AC: "No shared in-memory structure exists whose loss takes down more than one account").
- If the chosen container topology relies on PID 1 reaping, add a smoke assertion that the trading-subprocess container runs an init (`init: true` present, or pid 1 reaps) — otherwise the router shape leaks zombies across restarts.

---

### arq (job queue)

**Versions:** ours = 0.26.0+, latest = 0.28.0 (maintenance-only).

**Relevance:** **The live-supervisor does NOT use arq.** It is its own long-lived process (`python -m msai.live_supervisor`, `__main__.py:774-779` → `asyncio.run(_async_main())`), driven by the `LiveCommandBus` (Redis Streams), not arq. arq is used by the backtest/research/portfolio/ingest workers (`workers/`). PR 2 does not touch arq.

**Deprecations:** arq is in maintenance-only mode (no new features) — a standing project risk, not a PR-2 risk.

**Sources:**

1. [arq docs](https://arq-docs.helpmanual.io/) — accessed 2026-05-31
2. [arq GitHub](https://github.com/python-arq/arq) — accessed 2026-05-31

**Design impact:** No impact — the supervisor's process model is independent of arq. Do NOT model the per-account owner as an arq worker; the supervisor is a bespoke long-lived loop and PR 2 should stay within that model.

**Test implication:** Standard coverage sufficient — no arq surface changes.

---

### redis-py / Redis Streams (LiveCommandBus)

**Versions:** redis-py 5.2.0+. The command bus is a bespoke Redis-Streams consumer-group implementation (`services/live_command_bus.py`).

**Current shape (verified):**

- Single stream `msai:live:commands` + single consumer group `live-supervisor` (`live_command_bus.py:66-74`). All supervisor instances join the one group; each entry is delivered to exactly one consumer (Redis Streams group semantics).
- PEL recovery via `XAUTOCLAIM` on every `consume()` start + every `recovery_interval_s` (`:375-453`); poison-message DLQ after `MAX_DELIVERY_ATTEMPTS=5` (`:95-100`).
- **A per-account stream helper already exists but is unused:** `command_stream_for_account(account_id)` → `msai:live:commands:{account_id}` (`:82-92`), explicitly documented as "PR 1 only adds the helper; producer/consumer adoption is deferred to **PR 2** (per-account supervisor ownership)." This is a pre-built seam for PR 2.

**Recommended pattern:** Redis Streams consumer groups give per-stream PEL isolation. Splitting commands onto **per-account streams** (the helper) with **per-account consumer groups** means each account-owner consumes only its own stream — a crash/restart of one owner leaves the other accounts' streams + PELs untouched, and `XAUTOCLAIM` redelivery is naturally scoped to the account.

**Sources:**

1. Codebase: `services/live_command_bus.py:66-100,375-453` — read 2026-05-31
2. [Redis Streams consumer groups (XAUTOCLAIM)](https://redis.io/docs/latest/develop/data-types/streams/) — general reference (consumer-group + XAUTOCLAIM semantics)

**Design impact (BOTH shapes):**

- **Per-account PROCESSES shape:** Each owner consumes `command_stream_for_account(account_id)` with its own consumer group/name. This is the cleanest mapping — the ownership boundary is also the command-routing boundary, and the existing PEL/DLQ machinery applies per account for free. The API producer must publish START/STOP for an account onto that account's stream (today it publishes to the global stream).
- **Thin-ROUTER shape:** The router can keep the single global stream and fan commands out to nodes, OR adopt per-account streams as a pure routing convenience. Per-account streams are still beneficial (isolated PEL/redelivery) but less essential because the router isn't a per-account fault domain. The `stop_wrong_host` / cross-host guard already in `stop()` (`process_manager.py:1163-1204`) shows the codebase already reasons about "command routed to the wrong owner" — per-account streams make that guard largely unnecessary within an account.

**Test implication:**

- Test that a STOP for account B published during account A's owner restart is consumed and ACKed by B's owner (not lost, not consumed by A) — i.e. per-account stream isolation holds across an owner restart (US-1 AC: "Account B's `/drain/{B}`, `/resume/{B}`, and fleet `/kill-all` all behave identically during and after A's owner restart").
- Test XAUTOCLAIM redelivery is scoped: a command left in account A's PEL when A's owner dies is reclaimed by A's restarted owner, and account B's PEL is never touched.

---

## Existing Codebase Primitives (confirmed shape — read, not web-researched)

| Primitive                                                   | File:line                                              | Current shape relevant to PR 2                                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ProcessManager.handles` (the shared map to split)          | `process_manager.py:160`                               | `dict[UUID, mp.process.BaseProcess]`, deployment_id-keyed, **single map owned by the one supervisor** — the structure whose loss = fleet-wide blast radius.                                                                                                                                                                                                |
| `reap_loop` / `reap_once`                                   | `process_manager.py:835-922`                           | Polls `self.handles` for exits; meaningful only from the spawning parent. Empty after restart.                                                                                                                                                                                                                                                             |
| `spawn` (phases A/B/C)                                      | `process_manager.py:177-619`                           | INSERT-spawn-UPDATE; **already re-checks fleet halt + `account_halt_key` twice**. Auto-restart must reuse this path, not bypass the halt checks.                                                                                                                                                                                                           |
| `stop` (pid fallback)                                       | `process_manager.py:1125-1228`                         | Falls back to `row.pid` + `os.kill` when handle map empty → the existing "re-adopt by pid+DB" pattern. Has a cross-host guard.                                                                                                                                                                                                                             |
| `watchdog_loop` (startup-row killer, bounded by wall-clock) | `process_manager.py:928-1060`                          | SIGKILLs wedged `starting`/`building` rows; hostname-scoped. A model for a bounded auto-restart guard.                                                                                                                                                                                                                                                     |
| `kill_all` semantics                                        | `api/live.py:1771-1779` + `halt_keys.fleet_halt_key()` | 4-layer; Redis fleet halt latch + active-row sweep. Fleet stop = fan-out, not one shared handle.                                                                                                                                                                                                                                                           |
| `HeartbeatMonitor` (post-startup stale sweep)               | `heartbeat_monitor.py:103-150`                         | SOLE authority for `ready`/`running`/`stopping` rows; flips stale→failed. The cross-restart liveness signal. The per-account health field (US-3) reads `last_heartbeat_at`.                                                                                                                                                                                |
| Account-scoped halt latch                                   | `halt_keys.py:33-48`                                   | `account_halt_key(account_id)` — keyed by `account_id` NOT `ib_login_key`. **MUST gate auto-restart** (US-2 + §6).                                                                                                                                                                                                                                         |
| Per-account command stream helper                           | `live_command_bus.py:82-92`                            | `msai:live:commands:{account_id}` — pre-built for PR 2 adoption.                                                                                                                                                                                                                                                                                           |
| `LiveNodeProcess` model                                     | `models/live_node_process.py`                          | Per-restart run row: `pid` (nullable), `host`, `last_heartbeat_at`, `status`, `failure_kind`, `gateway_session_key`. **This DB row is what survives a supervisor restart** → the substrate for re-adoption and for the US-3 health field. Has the partial unique index `uq_live_node_processes_active_deployment` (at-most-one active row per deployment). |
| `/live/status` response                                     | `api/live.py:2459-2558`                                | Already surfaces latest `LiveNodeProcess` per deployment incl. `last_heartbeat_at`. US-3 extends this with a per-account owner-health field — additive.                                                                                                                                                                                                    |
| Supervisor shutdown contract                                | `main.py` module docstring                             | Supervisor does NOT SIGTERM running children on shutdown; next start re-discovers via heartbeat-fresh rows. **This is already the thin-router behavior in miniature.**                                                                                                                                                                                     |
| Compose `live-supervisor` (single service today)            | `prod.yml:349-426`, `dev.yml:208-237`                  | ONE `live-supervisor` service (broker profile). PR 1's HVP drill added a second IB **gateway** but kept the SAME single supervisor → the exact shared-ownership boundary PR 2 must split. No `init: true` set. `broker-hvp` profile adds the HVP gateway only.                                                                                             |

---

## Not Researched (with justification)

- **FastAPI / Pydantic / SQLAlchemy / Next.js:** PR 2's API change is an additive field on the existing `/live/status` response (US-3) — no new endpoint contract, no schema-framework version concern. Standard usage; not a research target. UI change (if any) is a read-only health badge on existing live status — `frontend/package.json` untouched in substance.
- **Databento / IB adapter internals:** PR 2 does not change data sourcing or IB connection construction — it changes _who owns_ the subprocess, not what the subprocess does. The per-account Databento/IB-exec topology was PR 1's scope (`__main__.py` payload factory). Auto-restart re-uses the existing payload factory unchanged.
- **ib_async:** execution path unchanged by PR 2.
- **tenacity:** already a dep; evaluated inline under the multiprocessing section (not a fit for the long-lived restart-ceiling policy). No separate research needed.

---

## Open Risks

1. **Version skew on multi-account reconciliation (HIGH-VALUE FLAG).** We are on **1.223.0**; the multi-account reconciliation isolation bugs ("conflating / collapsing positions across accounts on the same instrument," "retry/throttle leaking across accounts") were fixed in **1.226/1.227**. Our per-subprocess-per-account topology side-steps them _because each account gets its own kernel/exec-engine/reconciliation scope_. **Risk materializes only if a design ever collapses multiple accounts into one TradingNode/kernel.** Phase 3 should explicitly rule that out, and a future Nautilus-upgrade PR should pick up 1.227's fixes before any single-node-multi-account experiment. (Not a PR-2 blocker; a guardrail.)
2. **Single-VM memory envelope vs. N supervisor processes (per-account-PROCESSES shape).** PRD §5: Standard_D4ds_v6 = 16 GB, already near ceiling with the broker fleet. If owners are separate compose services, the host must budget N owner processes + N gateways + N trading subprocesses. Needs a Phase-3 capacity analysis (PRD explicitly defers the number). The thin-router shape avoids the per-account-owner-process cost but trades it for a heavier re-discovery/liveness burden.
3. **PID 1 / zombie reaping for the thin-router shape.** The trading-subprocess container does NOT set `init: true` today. If the router shape lets subprocesses outlive the router routinely, an exited-but-unreaped subprocess becomes a zombie the router can't reap from a fresh process. Mitigation (add `init: true` / tini, or have the supervisor be a robust reaper) is a compose change that must land in the same PR (NO BUGS LEFT BEHIND).
4. **`reconciliation_startup_delay_secs = 10.0` floor on restart latency.** A restarted account's node has a ~10s post-reconciliation stabilization delay before continuous reconciliation/order acceptance. US-2's "auto-restarts and reconciles" is correct, but operator expectations for _how fast_ an account self-heals should account for reconciliation time + this 10s floor. Surface this in the US-2 health states (e.g. a `recovering` substate) rather than implying instant recovery.
5. **Re-adoption is DB-row + pid re-discovery, never Python-object re-attach.** A restarted owner cannot regain `Process.is_alive()`/`exitcode` for a child it didn't spawn — it must use `os.kill(pid,0)`/`/proc`/heartbeat. The existing `stop()` pid-fallback proves the pattern; the auto-restart + health-read paths must adopt the same discipline and never assume the handle map is authoritative after a restart.
6. **Nautilus is permanently "Beta".** Every release is tagged Beta; "latest stable" = newest release line (1.227.0). Treat any upgrade as a real migration (config builder-pattern changes in 1.226 affect some Rust configs; verify `TradingNodeConfig`/`LiveExecEngineConfig` Python construction still matches at upgrade time). Not in PR-2 scope, but the version gap (1.223→1.227) is widening.
