# PR 2 — Per-Account Supervisor Ownership Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

> **REVISED 2026-05-31 after council #2 (container-topology ruling → Opt 4 refined).** The
> prior in-process `AccountSupervisor + SupervisorLease + RestartBudget + generation-token`
> fencing design was REJECTED as over-engineered for N=2. This plan implements per-account
> ownership _semantics_ in the existing single supervisor + the F6 node-side-halt P0 + the
> binding deploy contract. Per-account containers (Opt 1) are DEFERRED to the 2-VM split.
> See `docs/decisions/multi-account-broker-fleet.md` §"Addendum 2026-05-31 — council #2".

**Goal:** (1) isolate a single account's TradingNode crash so it can't affect other accounts and self-heals via halt-gated, bounded auto-restart; (2) make a halt a real kill-switch on a running node even when the supervisor is down (live Redis re-check, fail-closed); (3) surface per-account supervisor/restart-authority health; (4) document + enforce the binding production deploy contract — all within the existing single-VM single-supervisor container.

**Architecture:** Council-ratified **Opt 4 refined** (`## Approach Comparison`). The existing single `live-supervisor` keeps owning node `mp.Process` children, but: ownership _truth_ is DB+heartbeat (in-memory handles become an explicit non-authoritative `NodeHandleCache`); per-account command streams give command-path failure containment; the reaper + a startup re-scan drive halt-gated, staggered, reconcile-verified auto-restart; the running node re-checks the live Redis halt latch on the order path (fail-closed). NO in-process lease/generation fencing (the active-live deploy gate F5 prevents two-supervisor overlap). Per-account containers deferred to the 2-VM split.

**Tech Stack:** Python 3.12, NautilusTrader 1.223.0 (reconciliation verified via `kernel.trader.is_running`), Redis (per-account command streams + halt latches + reaper/router heartbeat), PostgreSQL (LiveNodeProcess rows + additive migration), Docker Compose (`init:true` for child reaping), FastAPI (/live/status), Typer (`msai live status`), existing alerting service.

---

## Approach Comparison

> Final — superseded twice by council. Council #1 (Shape A vs B) → "Shape B refined". Council #2 (container-topology, after the plan-review loop surfaced a P0) → **Opt 4 refined**, which is the binding outcome below.

### Chosen Default (post council #2)

**Opt 4 refined — per-account ownership semantics in the existing single supervisor + node-side live halt + binding deploy contract.** Node-crash isolation (already largely present — supervisor survives a child crash) + halt-gated bounded auto-restart + startup re-scan + per-account command streams (failure containment) + the F6 node-side-halt P0 fix + per-account health + honest `FleetRouter`/`NodeHandleCache` naming. NO in-process lease/generation fencing, NO per-account containers.

### Best Credible Alternative

**Opt 1 — per-account containers** (the council's ratified long-term container-per-account vision). Deferred: launch-model rewrite (nodes become orchestrator-launched containers, not `mp.Process` children) + material 16GB-VM OOM risk. Migration trigger documented: the 2-VM split, OR fleet > ~4-5 accounts, OR a deliberate-redeploy incident the deploy contract didn't cover.

### Scoring (fixed axes)

| Axis                  | Default (Opt 4 refined)                                                                | Alternative (Opt 1 per-account containers) |
| --------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------ |
| Complexity            | M (mostly hardening existing seams)                                                    | H (launch-model rewrite)                   |
| Blast Radius          | L for node crashes; container-recreate covered by deploy contract + staggered recovery | L (true per-account isolation)             |
| Reversibility         | H (no new launch model; per-account containers later reuse US-2/US-3/halt-fix)         | M                                          |
| Time to Validate      | M (~4-6 days)                                                                          | H (~8-12 days + capacity profile)          |
| User/Correctness Risk | M (supervisor-crash = container recreate, honestly scoped + deploy-gated)              | M-H (16GB OOM-kill of a live node)         |

### Cheapest Falsifying Test

F6 node-side-halt: with the supervisor stopped, set the Redis halt latch and confirm a running node blocks its next order submit (live re-check), and fail-closed when Redis is unreachable. **< 30 min** as an integration test (T12). The decisive deploy facts (F4 broker-excluded, F5 active-live refusal) were verified against the code during council #2.

## Contrarian Verdict

**COUNCIL ×2.** Council #1 (gate Contrarian OBJECT → full council) → Shape B refined. Council #2 (plan-review loop surfaced the container-topology P0 → re-escalation) → **Opt 4 refined**, 1 APPROVE-equiv + 4 CONDITIONAL, 1 OBJECT (Contrarian, resolved by making F4/F5 a binding documented contract + fixing F6). Hawk + Maintainer's Opt-1 (per-account containers) deferred to the 2-VM split. Unanimous: F6 node-side cached-halt is a real-money P0. The 11→(now simpler) conditions map to tasks T1–T13, cited inline. Full verdicts: decision-doc council #2 addendum.

---

## File Structure

- **Modify** `backend/src/msai/services/nautilus/risk/risk_aware_strategy.py` — F6: OVERRIDE `submit_order`/`submit_order_list`/`modify_order` (unbound base call, no recursion) with a reduce-only-aware gate; replace `_halt_flag_cached: bool` with a `tuple[bool,float]|None` staleness cache fed by a ≤1s background task; retire `submit_order_with_risk_check` to a thin alias. (T2 — the P0.)
- **Modify** `backend/src/msai/services/nautilus/trading_node_subprocess.py` — T2: in the existing `on_post_build` hook (after `node.build()`, before `node.run_async()`) enforce live-strategy halt-awareness with an explicit `if not isinstance(strategy, RiskAwareStrategy): raise` (fail-closed `SPAWN_FAILED_PERMANENT`, NOT `assert`) + wire the PR-2 halt collaborators only: the halt-refresh background task + one immediate pre-run refresh, and a best-effort `_audit`. (NOT `_risk_limits`/`portfolio` — those + the position/exposure suite are PR-5; see T2 halt-only scope.) **Also T7** (`os.setsid` as the first line of the child entrypoint) — T2 and T7 BOTH write this file → serialize (see Dispatch Plan).
- **Modify** `backend/src/msai/models/live_node_process.py` — additive columns: `auto_restart_paused`, `auto_restart_pause_reason`, `consecutive_respawn_failures`, `last_restart_at`.
- **Create** `backend/alembic/versions/2026_05_31_*_pr2_restart_authority_fields.py` — additive migration (nullable/defaulted only).
- **Create** `backend/src/msai/live_supervisor/restart_policy.py` — small DB-backed bounded-restart guard (`decide(account_id, deployment_id) -> RESTART | BACKOFF(delay) | PAUSED`), halt-latch-gated, `max-concurrent-respawn=1`. No lease, no generation token.
- **Modify** `backend/src/msai/live_supervisor/process_manager.py` — rename `ProcessManager`→`FleetRouter`, `handles`→`NodeHandleCache` (explicit non-authoritative cache); auto-restart branch in `_on_child_exit`; account-scoped reaper handling; account-scoped logging.
- **Modify** `backend/src/msai/live_supervisor/main.py` — per-account command-stream consumers with exception boundaries (one account's command failure can't stall others); startup re-scan of `failed`/stale active deployments → halt-gated auto-restart; publish reaper/router heartbeat.
- **Modify** `backend/src/msai/live_supervisor/__main__.py` — wire the startup re-scan + per-account consumers + heartbeat.
- **Modify** `backend/src/msai/services/live_command_bus.py` — adopt `command_stream_for_account` producer/consumer; retire/guard the global-stream consumer to prevent double-processing.
- **Modify** `backend/src/msai/api/live.py` — `/live/status` per-account restart-authority health + top-level `router_heartbeat_age_s`; START/STOP publish to the per-account stream.
- **Modify** `backend/src/msai/schemas/live.py` — status response schema fields.
- **Modify** `backend/src/msai/cli.py` — `msai live status` restart-authority health.
- **Modify** the alerting surface (`backend/src/msai/services/alerting.py` or equivalent) — `router_heartbeat_age_s > 30` + flat-and-unmonitored alerts.
- **Modify** `docker-compose.dev.yml` + `docker-compose.prod.yml` — `init: true` on `live-supervisor` (child reaping for node `mp.Process` crashes) + `os.setsid` spawn.
- **Modify** `frontend/src/components/live/strategy-status.tsx` + `frontend/src/lib/api.ts` — restart-authority health column (+`data-testid`).
- **Modify** `docs/how_to_deploy.md` (or the deploy runbook) + the release checklist — the binding deploy contract (broker excluded from routine deploy + active-live refusal). **Decision-doc addendum already written.**

---

## Tasks

> TDD per task (Red→Green→Refactor→commit). Council condition cited inline.

### Task 1: Additive migration + model fields

**Files:** `backend/src/msai/models/live_node_process.py`; `backend/alembic/versions/2026_05_31_*_pr2_restart_authority_fields.py`; Test `backend/tests/integration/test_pr2_migration.py`.

Additive columns (no `owner_generation` — no fencing in Opt 4 refined): `auto_restart_paused BOOLEAN NOT NULL DEFAULT false`, `auto_restart_pause_reason TEXT NULL`, `consecutive_respawn_failures INTEGER NOT NULL DEFAULT 0`, `last_restart_at TIMESTAMPTZ NULL`. Leave `gateway_session_key` unchanged.

- [ ] Failing test: migration applies + downgrades cleanly (additive-only); model round-trips new fields.
- [ ] Generate + verify additive-only; `alembic upgrade head` green. Commit.

### Task 2: Node-side live halt — enforced for ALL live strategies (F6) — **REAL-MONEY P0** [council #2 unanimous; Codex iter-2 P0]

**Files:** `backend/src/msai/services/nautilus/risk/risk_aware_strategy.py` (override the submit cpdefs + staleness cache), `backend/src/msai/services/nautilus/trading_node_subprocess.py` (`on_post_build` `isinstance` enforcement + collaborator/background-task wiring, before `node.run_async()`), `strategies/example/smoke_market_order.py`, `strategies/example/ema_cross.py`, `CLAUDE.md` (correct the "4-layer kill-all" wording — see implement step); Test `backend/tests/unit/services/nautilus/risk/test_node_side_live_halt.py` + `backend/tests/unit/services/nautilus/test_live_strategy_halt_enforcement.py`.

**Enforcement mechanism — the mixin OVERRIDES the public submit cpdefs (P0 — Claude+Codex iter-4, cross-validated against code).** The iter-3 plan said "route all submission through the mixin's single `submit_order_with_risk_check`." That is WRONG against the actual code: `submit_order_with_risk_check` (`risk_aware_strategy.py:167`) is a SEPARATE helper that itself calls `self.submit_order(order)` — it does NOT override `submit_order`. The two example strategies inherit bare `Strategy` and call `self.submit_order(order)` **directly** (`smoke_market_order.py:112`, `ema_cross.py:182`) — so today they get **zero node-level halt enforcement** (the F6 hole, confirmed). So the corrected design:

- **`RiskAwareStrategy` OVERRIDES `submit_order`, `submit_order_list`, `modify_order`** as Python `def` methods. `Strategy.submit_order`/`submit_order_list`/`modify_order` are Cython `cpdef`, so a Python subclass override shadows them for the strategy's own Python-level calls (e.g. from `on_bar`). The override does the gate, then calls the REAL Nautilus impl via the **explicit unbound base call** `Strategy.submit_order(self, order, ...)` — NOT `self.submit_order(...)` (which would infinitely recurse). This means a strategy calling `self.submit_order(order)` directly (as the examples do) is now gated with **no change to the strategy body** beyond the base class.
- **BACKTEST-SAFE ARMING — the halt gate is LIVE-ONLY (P2 — Codex iter-7).** The SAME example strategies (now `RiskAwareStrategy`, calling `self.submit_order` directly) run in BACKTESTS too, loaded via `ImportableStrategyConfig` through `backtest_runner.py:389` — which has NO `on_post_build` hook and NO live halt latch. If the override's fail-closed `None`-cache logic ran in backtests, EVERY backtest/smoke would block all opening orders. So the halt gate must be **armed only in live**: add a `_halt_gate_armed: bool = False` (class default). The live `on_post_build` wiring sets `_halt_gate_armed = True` (alongside starting the refresh task). **When NOT armed (backtest/non-live), the halt branch is INERT → always allow** (there is no live halt latch in a backtest). Fail-closed (`None`/stale → block) applies ONLY when armed. (Equivalently: a distinct "not armed" sentinel vs "armed-but-None".)
- **Gate logic per order — PR 2's MANDATORY check is the HALT only (scope clarification — Claude+Codex iter-5 P1):** if the order is reduce-only / `MARKET_EXIT`-tagged → **ALWAYS allow** (it only reduces risk). Else, **if the halt gate is not armed (backtest) → allow**; else evaluate the **halt** via the sync live cache (below); halted/None/stale → `_record_denial` (best-effort, see wiring) + return without submitting; not halted → call the unbound `Strategy.<method>(self, ...)`. **The position/exposure/daily-loss suite (`_run_risk_checks`' other checks) runs ONLY when `_risk_limits` is wired — which PR 2 does NOT do (per-account risk caps are an explicit PR-5 non-goal; `risk_aware_strategy.RiskLimits` has no live data path today, confirmed iter-5). So in PR 2 those checks are GUARDED (skipped when `_risk_limits is None`), preventing the `AttributeError` the reviewers flagged; PR 5 wires `_risk_limits` + the cumulative-batch simulation.** `submit_order_list` (PR 2): if ANY non-reduce-only leg is halted → deny the WHOLE list atomically (no partial submit); `modify_order` under halt is denied unless it targets a reduce-only/flatten order (a modify that could increase exposure is treated as opening). **PR-5 note:** when `_risk_limits` lands, `submit_order_list` must simulate CUMULATIVE per-instrument/per-venue risk across the whole non-reduce-only batch (current per-leg checks project current-portfolio + one order — Codex iter-5 P1); carry this forward to PR 5's risk-cap task.
- **`submit_order_with_risk_check` is RETIRED to a thin deprecated alias** (`return self.submit_order(order)`) so the gate is not run twice (the override now owns the single gated path). Update any internal callers/tests.
- **Reduce-only/flatten signal (VERIFIED iter-4):** Nautilus `market_exit()` calls `close_all_positions(..., tags=["MARKET_EXIT"], reduce_only=...)` → `close_position()` builds a `MarketOrder(reduce_only=..., tags=...)` (`strategy.pyx:1400/1416/1798`; `Order` exposes `is_reduce_only` + `tags`, `orders/base.pyx:208`). Detect a flatten/reduce-only order via `order.is_reduce_only or "MARKET_EXIT" in (order.tags or [])` — the SAME guard Nautilus uses internally (`strategy.pyx:861`). **Note on the internal flatten path:** `market_exit()`→`close_position()` calls `self.submit_order(...)` from Cython; whether that dispatches to our Python override is a cpdef-internal-dispatch detail — but it is SAFE either way: if it hits our override, the reduce-only branch allows it; if it bypasses to the C impl, Nautilus's own `_is_exiting` guard (`strategy.pyx:861`) still permits the reduce-only/MARKET_EXIT order. Opening orders from `on_bar` are Python-level calls → ALWAYS hit our override → halt-gated. (Verify the dispatch empirically in the T12 outage drill, but neither outcome is a safety hole.)
- **Bounded guarantee (Codex iter-3 P1, retained):** the gate covers the supported public submit API (`submit_order*` / `modify_order`). A strategy bypassing it via Nautilus internals (`self._manager.send_risk_command`, `self._msgbus`, hand-built commands) is OUT of the guaranteed surface — documented in the mixin docstring; a simple static lint for `_msgbus`/`_manager`/`send_risk_command` in `strategies/` rejects obvious bypasses. Do NOT overclaim "all order paths" (PRD US-1b).
- **Update the two example strategies to declare `class X(RiskAwareStrategy, Strategy)` — mixin FIRST (P2 — Codex iter-8)** so the gated override wins the MRO. (They already call `self.submit_order`, which now resolves to the override.) Today they are `class X(Strategy)`; the change is the base-class tuple with `RiskAwareStrategy` ahead of `Strategy`.

**Child-side mandatory-base-class enforcement (P1 — Claude iter-4: the check needs strategy CLASSES, which don't exist before `node.build()`).** The child imports strategies via `ImportableStrategyConfig` INSIDE `node.build()` (`trading_node_subprocess.py:761`) — there is no `strategy_cls` variable before build. Enforce in the **existing `on_post_build` hook** (`trading_node_subprocess.py:766`, awaited AFTER build but BEFORE `node.run_async()`): iterate the instantiated strategies (the same iteration `_wire_market_hours` already does at `:1429-1432`) and, for every live strategy, enforce — with an **explicit `if ...: raise SPAWN_FAILED_PERMANENT(...)`, NOT `assert` (Codex iter-6 P2: `python -O` strips `assert`)** — BEFORE `node.run_async()` (fail-closed — a non-halt-aware live strategy never trades).

**`isinstance` ALONE IS INSUFFICIENT — verify the override actually wins the MRO (P2 — Codex iter-8/9).** `RiskAwareStrategy` is a MIXIN that does NOT inherit `Strategy` (`risk_aware_strategy.py:145`). A class declared `class S(Strategy, RiskAwareStrategy)` (Strategy FIRST) PASSES `isinstance(s, RiskAwareStrategy)` but its MRO resolves `self.submit_order` to Nautilus's un-gated `Strategy.submit_order` → the halt gate is **silently bypassed**. The enforcement must therefore verify the gate is actually in effect, not just that the mixin is present:

- Check `isinstance(strategy, RiskAwareStrategy)` **AND that `RiskAwareStrategy` is the FIRST owner of each gated name in the MRO** — i.e. walking `type(strategy).__mro__`, the first class whose `__dict__` contains `submit_order` (and likewise `submit_order_list`, `modify_order`) must be `RiskAwareStrategy`. Reject (`SPAWN_FAILED_PERMANENT`) otherwise.
- **Why "first MRO owner", not `is not Strategy.submit_order` (P2 — Codex iter-9):** the weaker identity check accepts a subclass that RE-OVERRIDES the method with its own un-gated version (e.g. `class S(RiskAwareStrategy, Strategy): def submit_order(self, ...): return Strategy.submit_order(self, ...)`) — that subclass's `submit_order` is neither `Strategy.submit_order` nor `RiskAwareStrategy.submit_order`, so it would slip past the identity check while bypassing the gate. "First MRO owner must be `RiskAwareStrategy`" rejects BOTH the wrong base-class order (`Strategy` first) AND any subclass re-override. **Consequence (documented):** a live strategy may NOT override `submit_order`/`submit_order_list`/`modify_order` — the gate must be the effective implementation; strategies extend behavior in `on_bar`, not by wrapping the submit methods. (PR 5 can add a sanctioned gated-extension hook if ever needed.)

(`on_post_build`-pre-run is chosen over a manual pre-build `importlib` import because it reuses the existing strategy iteration and the node has not started trading yet — build alone places no orders.)

**Collaborator wiring — what PR 2 actually wires (P0/P1 — Codex iter-4 + Claude/Codex iter-5).** The original concern: `_run_risk_checks` derefs `_risk_limits` (`risk_aware_strategy.py:234`) and `_record_denial` derefs `_audit` (`:369`), but the child injects ONLY `_market_hours_check` today (`trading_node_subprocess.py:1431-1432`) → naive wiring crashes on first order. Resolved by the halt-only scope above:

- **`_risk_limits` / `portfolio`: NOT wired in PR 2** (no live data path exists — iter-5; per-account risk caps = PR 5). The position/exposure/daily-loss checks are guarded behind `_risk_limits is not None`, so leaving them unset is safe — the suite is simply skipped. **Precision (Claude iter-6 P1):** the current declarations are bare annotations with NO default (`_risk_limits: RiskLimits`, `_audit: OrderAuditWriter` — `risk_aware_strategy.py:157-158`), so `self._risk_limits` on an unwired instance raises `AttributeError`, NOT `None`. T2 MUST change these to defaulted optionals — `_risk_limits: RiskLimits | None = None` and `_audit: OrderAuditWriter | None = None` — so the `is not None` guard actually works. PR 5 adds the `TradingNodePayload` risk-limit field + source + wiring.
- **`_audit`: wired but BEST-EFFORT (Claude iter-5 P1).** Extend the `on_post_build` wirer to inject the `OrderAuditWriter`. Construct/resolve it OUTSIDE the swallowed `except Exception: pass` block (`trading_node_subprocess.py:1441-1661`) so an injection failure is visible, not silent. CRITICAL: `_record_denial` must be guarded so a missing/failed `_audit` logs a warning and continues — it MUST NOT raise and MUST NOT let the order through. The halt BLOCK is unconditional (fail-closed); auditing the denial is best-effort, never a precondition for blocking.
- **Halt-refresh state + background task: REQUIRED, wired in `on_post_build`** (the one mandatory new collaborator for the F6 gate) — see below.

**The halt check itself — sync-safe + background refresh wiring (P1 — Claude iter-2 + iter-4; name precision Claude iter-5 P2).** The halt gate is SYNCHRONOUS (`on_bar` hot path, no `await`). NO blocking Redis GET inline. Rename the class attribute `_halt_flag_cached: bool = False` (`risk_aware_strategy.py:159`) → `_halt_cache: tuple[bool, float] | None = None` and update its one reader (`risk_aware_strategy.py:205`, `if self._halt_flag_cached:`) to the new tuple-or-None logic. Add a module-level constant `HALT_CACHE_MAX_AGE_S: float = 2.0` in `risk_aware_strategy.py`. Semantics: `None` = "never fetched", age > `HALT_CACHE_MAX_AGE_S` = "stale". The sync gate treats `None` OR stale OR `True` as **halt → BLOCK opening orders, ALLOW reduce-only/flatten/cancel (fail-closed for new exposure)**. The cache is fed by a background asyncio task that reads `fleet_halt_key()` + `account_halt_key(account_id)` every ≤1s and writes `(value, monotonic())`.

- **Wiring (P1 — Claude iter-4: else the cache is `None` forever → node permanently blocks all opening orders):** in the live `on_post_build(node, payload, session_factory)`, **set `_halt_gate_armed = True`** (arms the gate — backtests never reach this hook, so they stay inert per the backtest-safe bullet above), create the background task via `asyncio.create_task(...)` on the running Nautilus loop, AND **`await` one immediate refresh BEFORE `node.run_async()`** so the cache is populated before the first bar (closes the cold-start window — Claude iter-4 P2). **Pass `payload.ib_account_id` (the `DU…`/`U…` id keyed by `account_halt_key`, per `halt_keys.py`) to the refresh task — NOT `ib_login_key` (Claude iter-9 P3)** so it reads the right account latch. Store the task handle on the strategy and cancel it on `on_stop`/dispose. Alert on a stale-cache-block so a Redis blip is visible.

**Rollout note (P1 — Claude iter-4: existing deployments break after T2).** Once the `on_post_build` `isinstance` check lands, ANY existing `live_deployment` whose strategy file still inherits bare `Strategy` will get `SPAWN_FAILED_PERMANENT` on its next (re)start. This is intentional (the mixin is mandatory for live), but the T11 release checklist MUST warn: before deploying PR 2, every live/queued strategy file must inherit `RiskAwareStrategy` (the two examples are updated here; any operator custom strategy must be too), else that account fails to start and needs a manual restart with updated code.

- [ ] Failing tests: (a) a plain `Strategy` subclass (no `RiskAwareStrategy`) is rejected in `on_post_build` with `SPAWN_FAILED_PERMANENT` before `node.run_async()`; (b) a `RiskAwareStrategy` subclass gets the gate on `submit_order` + `submit_order_list` + `modify_order` (direct `self.submit_order` call is gated; no recursion); (c) halt true + opening order → BLOCKED; denial recorded best-effort, and **a missing/raising `_audit` still BLOCKS** (does not crash, does not admit the order); (d) **halt true + reduce-only/`MARKET_EXIT` order → ALLOWED** (kill-all/drain flatten works under halt); (e) `submit_order_list` with a mix → atomic deny under halt; opening `modify_order` under halt → BLOCKED, reduce-only modify → ALLOWED; (f) Redis unreachable → cache `None`/stale → opening BLOCKED, flatten ALLOWED; (g) account-scoped halt blocks only that account, fleet halt blocks all; (h) **with `_risk_limits` UNSET (PR-2 default), a non-halted opening order is ALLOWED and does NOT `AttributeError`** (the position/exposure/daily-loss suite is guarded/skipped), and the halt-refresh task is wired in `on_post_build`; (i) the immediate pre-run refresh populates the cache so a first-bar order with no halt is ALLOWED (no cold-start false-block); (j) **BACKTEST-mode (gate NOT armed, no `on_post_build` wiring): opening orders flow freely even with the cache `None`** (the live-only arming means a backtest/smoke is never blocked — Codex iter-7 P2); (k) **MRO enforcement (Codex iter-8/9):** `class S(Strategy, RiskAwareStrategy)` (mixin last) is REJECTED at `on_post_build` (`SPAWN_FAILED_PERMANENT`) even though `isinstance` passes; **a subclass that re-overrides `submit_order` (`class S(RiskAwareStrategy, Strategy): def submit_order(...)`) is ALSO REJECTED** (first-MRO-owner is `S`, not `RiskAwareStrategy`); a correctly-ordered `class S(RiskAwareStrategy, Strategy)` with NO submit override is accepted.
- [ ] Implement the submit-cpdef overrides (unbound-base-call, no recursion) + `_halt_gate_armed` live-only arming (backtest-safe) + reduce-only-aware sync staleness gate + `_risk_limits`/`_audit` → defaulted-optional annotations + `on_post_build` `isinstance` enforcement + collaborator/background-task wiring (sets `_halt_gate_armed=True`) + immediate pre-run refresh + retire `submit_order_with_risk_check` to an alias + **update the mixin class docstring** (`risk_aware_strategy.py:125-128` still instructs the retired helper; the supported call is now `self.submit_order(...)`, gated by the override) + update example strategies + supported-API lint. Green + commit. **Correct the CLAUDE.md "4-layer kill-all" wording (the in-node order gate is live-backed + reduce-only-aware, not cached-Layer-4).**

### Task 3: `restart_policy` — simple bounded-restart guard [council: bounded, no infinite loop; no lease]

**Files:** `backend/src/msai/live_supervisor/restart_policy.py`; Test `backend/tests/unit/live_supervisor/test_restart_policy.py`.

DB-backed (T1 columns): `decide(...) -> RESTART | BACKOFF(delay) | PAUSED`. Rolling-window max-attempts; exponential backoff + jitter; after the ceiling set `auto_restart_paused=true` + reason. `record_success()` resets. Counter on the DB row (survives a container recreate). NO Redis lease, NO generation token (Opt 4 refined — F5 active-live deploy gate prevents two-supervisor overlap).

**Concrete defaults (P3 — Codex iter-8; resolves PRD §7 open question) — module constants, env-overridable:** `MAX_RESTART_ATTEMPTS = 5` consecutive failures within `RESTART_WINDOW_S = 1800` (30 min) → `PAUSED`; backoff `base = 10s`, factor 2, cap `300s` (5 min): 10→20→40→80→160→300, with ±25% jitter; `record_success()` (node reached `is_running` + reconciled) resets the counter + clears `auto_restart_paused`. Conservative on purpose — a real-money node should not hammer a recovering shared IB gateway; the 10s floor sits above `reconciliation_startup_delay_secs=10`. Values are constants in `restart_policy.py`, overridable via settings for the drill.

- [ ] Failing tests: N consecutive failures → PAUSED + `auto_restart_paused=true`; backoff grows with jitter bounds; success resets; counter survives a simulated supervisor restart (re-read from DB).
- [ ] Implement. Green + commit.

### Task 4: Per-account command streams + failure containment [council: one poison command can't stall all accounts]

**Files:** `backend/src/msai/services/live_command_bus.py`, `backend/src/msai/services/live/flatness_service.py`, `backend/src/msai/live_supervisor/main.py`, `backend/src/msai/api/live.py`; Test `backend/tests/unit/services/test_command_stream_per_account.py` + `backend/tests/integration/api/test_drain_killall_stop_per_account_stream.py`.

Adopt `command_stream_for_account(account_id)` (pre-built PR 1). The supervisor runs a per-account consumer task wrapped in an exception boundary so one account's command exception can't stall others.

**Per-account consumer lifecycle/discovery (P2 — Codex iter-7: how does a consumer exist before an account's first START?).** The current supervisor has ONE fixed bus consumer (`main.py:137`). PR 2 replaces it with per-account consumers. The supervisor must guarantee an account's consumer is running BEFORE any command for that account is published. Mechanism for PR 2's static account pool:

1. **At supervisor startup**, enumerate the known accounts — the union of (a) the static configured account set (the PR-1 Shape-B compose accounts, e.g. LVP+HVP) and (b) `DISTINCT account_id` from active/recent `live_deployments` — and start one per-account consumer task (idempotent: starting a consumer for an already-consumed account is a no-op via the consumer-group `XGROUP CREATE ... MKSTREAM` + `NOGROUP`-safe create).
2. **On a START for an account with no running consumer** (defensive, covers a just-added account before PR 3's dynamic CRUD): `/start-portfolio` ensures the per-account stream+group exist (idempotent create) before publishing, and the supervisor lazily starts the consumer when it first sees the account in its startup scan or via a lightweight "known-accounts" refresh on each reaper pass. **PR 2 relies on the static pool**; dynamic per-account consumer spin-up at runtime (no supervisor restart) is hardened in PR 3 (operator add-account). Document this boundary.
3. Producer publish (`/start-portfolio`) creates the stream/group idempotently so a START is never lost to a missing stream even if the consumer attaches a beat later (Redis Streams retain the entry; the consumer reads it on attach).

**ALL producer paths must migrate before the global consumer is retired (Codex iter-2 P1 + Claude iter-2 P1-B):** today `/stop` (`live.py:1553`), `/kill-all` (`live.py:1802`), and `/drain` (`live.py:2049`) publish via `coalesce_or_publish_stop_with_flatness(...)` (`flatness_service.py:44`), which uses the passed `LiveCommandBus` → the GLOBAL stream (`live_command_bus.py:217`). So `STOP`, `STOP_AND_REPORT_FLATNESS`, kill-all, and drain ALL flow through the global stream today. Migration:

1. `coalesce_or_publish_stop_with_flatness` + the START publish + kill-all/drain loops publish to `command_stream_for_account(deployment.account_id)` (one publish per deployment, keyed by its account).
2. The global `bus.consume()` in `run_forever` is retired ONLY AFTER (1) lands — verified by a test that `/stop`, `/drain`, `/kill-all`, and `STOP_AND_REPORT_FLATNESS` are each consumed+ACKed from the per-account stream with the global consumer disabled.
3. **`_supervisor_is_alive` migration (Claude iter-3 P2-NEW-A):** `_supervisor_is_alive` (`api/live.py:159`) gates `/start-portfolio` (503 if dead) by probing the GLOBAL stream's consumer-group activity (`xinfo_consumers`). If T4 retires the global consumer, this probe returns false-negative ("supervisor dead") and ALL `/start-portfolio` calls 503 even when the supervisor is up. Resolve in T4 (not deferred): either (a) keep a no-op supervisor consumer registered on the global stream's group for the probe, OR (b) migrate `_supervisor_is_alive` to read the `router_heartbeat` Redis key (the same signal T8 surfaces as `router_heartbeat_age_s`). **Prefer (b)** — publish `router_heartbeat` in `run_forever` (a few lines, also needed by T8/T9) and switch the probe to it; this removes the global-stream dependency entirely. If (b), T4 must land the `router_heartbeat` publish + probe switch atomically with the global-consumer retirement.

- [ ] Failing tests: a STOP for account B published during an account-A command exception is consumed+ACKed by B; A's exception doesn't stall B; **a START + `/stop` + `/drain` + `/kill-all` + `STOP_AND_REPORT_FLATNESS` each deliver via the per-account stream with the global consumer OFF (none stranded)**; **a START published to an account whose consumer attaches a beat later is still consumed (idempotent stream/group create — not lost)**; **supervisor startup enumerates the static pool + active-deployment account_ids and starts a consumer per account**; no double-processing; **`_supervisor_is_alive` returns true (via `router_heartbeat`) with the global consumer retired and the supervisor up** (the 503 gate doesn't false-trip).
- [ ] Implement. Green + commit.

### Task 5: Rename `ProcessManager`→`FleetRouter`, `handles`→`NodeHandleCache` [Maintainer]

**Files:** `process_manager.py` (+ rename to `fleet_router.py`), all importers incl. `_PhaseAOutcome` references in tests; full suite stays green (pure rename).

Make the non-authoritative cache semantics explicit in the type name + docstring ("DB+heartbeat is truth; this cache is empty after a restart"). Distinct commit.

- [ ] Rename; update importers (grep `ProcessManager`, `_PhaseAOutcome`, `.handles`); full unit suite green. Commit (pure rename).

### Task 6: US-2 auto-restart in the reaper + startup re-scan [PRD US-1/US-2; halt-gated; closes Claude 1-A/7-A]

**Files:** `fleet_router.py` (post-T5 rename of `process_manager.py`; `_on_child_exit`), `main.py`/`__main__.py` (startup re-scan); Test `backend/tests/unit/live_supervisor/test_auto_restart.py`, `test_startup_rescan.py`.

**Prerequisite (Claude iter-5 P3):** depends on T4 (table: T1,T3,T4,T5) — T4's per-account command-consumer exception boundary + producer migration must already be in `main.py` before T6 adds the startup re-scan to the same file (shared-file serialization `main.py`: T4 → T6 → T10).

(a) `_on_child_exit`: after recording terminal state, decide restart via the **halt gate FIRST (at the decision point, before queuing any restart — Claude iter-1 3-A)**. **The gate MUST check `fleet_halt_key() OR account_halt_key(account_id)` — NOT just the account key (P2 — Codex iter-8):** `/kill-all` sets ONLY the FLEET halt key (`api/live.py:1752`) while `/drain` sets the ACCOUNT key (`api/live.py:2001`), so an account-only gate would let auto-restart fight a fleet emergency kill-all. Restart is suppressed if EITHER key is set. Then `restart_policy.decide()` + `max-concurrent-respawn=1` (reuse `_phase_a_reserve_slot`). The backoff sleep must be **cancellable** — a fleet OR account halt arriving during backoff abandons the restart (Claude iter-1 3-B; use an event-wait, not bare `asyncio.sleep`). Reconciliation verified via `startup_health.wait_until_ready()` (`kernel.trader.is_running`); restarted node does NOT accept orders until reconciled (surface a `recovering` substate; honor `reconciliation_startup_delay_secs=10`). (b) **Startup re-scan** (closes the gap where a node died while the supervisor was down → `NodeHandleCache` empty → `_on_child_exit` never fires): on supervisor start, scan this fleet's `failed`/stale active deployments with `auto_restart_paused=false` and re-evaluate restart eligibility through the SAME halt-gate + policy.

**Dup-restart race (Claude iter-2 P2-A):** the startup re-scan AND per-account PEL recovery can both try to restart the same dead deployment. The existing partial-unique-index (`uq_live_node_processes_active_deployment`, Phase A `ALREADY_ACTIVE` path) serialises them — the second attempt ACKs idempotently. Make `restart_policy` row-locked/idempotent so the duplicate doesn't double-count an attempt. **Do NOT add a separate distributed lock** — the DB unique index is the serialisation point.

**(c) COMMON-CRASH REAPER FIX — council #3 verdict (2026-05-31), REAL-MONEY P1.** The dual-engine code review found that `_on_child_exit`'s SELECT filters `status IN (starting,building,ready,running,stopping)` and early-returns BEFORE the `_maybe_auto_restart` dispatch — but the subprocess writes its OWN terminal `failed` row LAST in its finally (`trading_node_subprocess.py` `_mark_terminal`, "Terminal write LAST") before exiting. So for the COMMON crash the row is already `failed` at reap time → SELECT misses it → auto-restart NEVER fires at runtime (only SIGKILL/OOM, which leave the row stale at `running`, reach the dispatch; the startup re-scan is one-shot, not periodic). Council (1 APPROVE + 4 CONDITIONAL, 0 OBJECT) ruled **hybrid Option A** — keep terminal-write-LAST, fix the reaper:

1. **Reaper classifies the latest terminal row.** Widen/branch `_on_child_exit` to also fetch the latest `failed` (and `stopped`) row for the exited deployment **under `SELECT ... FOR UPDATE`** (idiomatic here — `_refail_stranded_restart`/Phase A already use `.with_for_update()`), classify by `exit_code` + halt-gate + stop-intent, and route an eligible non-zero exit into the SAME `_maybe_auto_restart` (do NOT duplicate the restart path).
2. **Durable operator-stop intent = a node-scoped additive column `live_node_processes.stop_requested_at TIMESTAMPTZ NULL`** (reject Redis-TTL — fail-open on a Redis blip would resurrect a stopped node; reject deployment-row intent — less precise, clearing semantics). `/stop` (`fleet_router.stop()` / `api/live.py`) MUST, **under a `FOR UPDATE` row lock, set `stop_requested_at` THEN commit THEN signal** (set-intent-BEFORE-signal). The reaper suppresses restart whenever `stop_requested_at IS NOT NULL`, **even on a non-zero exit** (a `/stop` whose graceful shutdown then crashes must NOT be resurrected — F5). This + the `FOR UPDATE` on the reaper's classify read closes the P2 stop-vs-self-crash race (the reaper either sees the committed intent or blocks until `/stop` commits it).
3. **Idempotency sentinel — additive column `live_node_processes.restart_dispatched_at TIMESTAMPTZ NULL`.** Set it under the same `FOR UPDATE` BEFORE dispatching, so the reaper never re-dispatches against a still-`failed` terminal row across passes (the partial unique index protects the active slot, not a repeated decision on a terminal row).
4. **Per-decision structured logging (Hawk, in-scope):** every reaper decision emits a structured log — `auto_restart_dispatched`, `auto_restart_suppressed_operator_stop`, `auto_restart_suppressed_by_halt`, `auto_restart_paused`, and a NEW `auto_restart_skipped_no_row` (reaper saw a dead node but took no action) — so an operator can alert on a silent no-op. (Dovetails with T10 account-scoped logging.)
5. **Migration:** add `stop_requested_at` + `restart_dispatched_at` as a NEW additive migration chained after T1's `d4e5f6a7b8c9` (both nullable; additive-only). Update the `LiveNodeProcess` model.
6. **`record_failure` attempt counting stays INSIDE `_phase_a_reserve_slot`** (Hawk) — never count in the reaper, or the terminal-row path double-counts the ceiling.

Full council verdict + minority report (Pragmatist's FOR-UPDATE-not-needed dissent + Simplifier's deployment-row alternative, both overruled) is durable in the session transcript; the binding conditions are encoded here.

**(c.1) Code-review FIX 1 (P1) + FIX 2 (P2) + cross-path cleanup-invariant audit.** Unifying invariant across all supervisor cleanup paths: (1) CLEANUP of a dead row (flip→`failed`, leave the active unique-index set) is UNCONDITIONAL; (2) respawn-ELIGIBILITY (pause / `stop_requested_at` / halt / recoverable-kind / ceiling) is the separately-gated decision; (3) a cleanup/recovery op targets a SPECIFIC owned row by id, never "the latest row". **FIX 1:** removed the `auto_restart_paused.is_(False)` predicate from the rescan Step-1 stale-active→`failed` flip (`_load_rescan_candidates`) — a dead PAUSED stale-active row was stuck-active forever (blocked future starts); Step-2 respawn still gates `auto_restart_paused=False` + `stop_requested_at IS NULL` + recoverable-kind, so a flipped-but-paused row leaves the active set but is NOT respawned. **FIX 2:** `_clear_sentinel_and_refail_after_giveup` now targets the reaper's OWNED row (`restart_dispatched_at`-stamped row id threaded through `_on_child_exit`→`_schedule_restart_task`→`_run_restart_task`) instead of `ORDER BY started_at DESC LIMIT 1` — a concurrent rescan/operator-retry that started a fresh active row is no longer clobbered (the parent deployment is re-failed only when no newer active row exists). Audit confirmed unconditional cleanup at `watchdog_once` (starting/building, host-scoped) and `HeartbeatMonitor._mark_stale_as_failed` (ready/running/stopping); `_refail_stranded_restart`'s latest-row targeting is correct because it runs INLINE in the attempt that created that row (latest IS owned).

**Deferred (Phase 2 — 2-VM cross-host split).** KEPT the `stop_requested_at IS NULL` filter on the rescan Step-1 flip (Council #3 F5 preserved, test `test_rescan_ignores_stop_requested_stale_active_row` unchanged). In PR-2's Phase-1 single-VM topology a dead `stop_requested_at` stale-active row is already cleaned by the HeartbeatMonitor (ready/running/stopping, unconditional) + the watchdog (starting/building, host-scoped — same host on a single-VM supervisor restart), and is never respawned (Step-2 gates `stop_requested_at`), so no stuck row remains. In a future cross-host Phase-2 (2-VM) topology, a `stop_requested_at` starting/building row orphaned on a DEAD host is not reached by the host-scoped watchdog — that topology will need the rescan to flip it unconditionally. Deferred with the Phase-2 2-VM split.

**(d) UNIFIED RUNTIME RECOVERY MODEL — council #4 verdict (2026-06-01), OPT C.** Across code-review iterations the SAME "transient failure strands the account flat-and-unmonitored" bug leaked through 3 different dispatch paths (command-stream-START transient ACK-dropped; reaper-dispatch transient EXCEPTION sentinel-stuck; reaper-dispatch transient NO-ACK `False` return [`CONCURRENT_STARTUP`/transient payload-factory] not retried). Root cause: recovery was per-path/event-driven with NO state-driven backstop, and the rescan was one-shot at startup. Council (APPROVE/CONDITIONAL ×5, 0 OBJECT; Simplifier+Maintainer dissent to delete the fast retry / external-watcher-only SPOF — OVERRULED) ruled **OPT C**:

1. **Periodic reconciling rescan (the authoritative backstop).** Wire `rescan_for_restart` onto a **30s interval loop** in `run_forever` (sibling of the existing background loops), **first pass immediate at boot**, and **SUBSUME the one-shot startup rescan** (retire the one-shot — ONE rescan code path at boot AND runtime, NOT two). It re-drives every `failed`+eligible deployment (non-halted fleet-OR-account fail-closed, `auto_restart_paused=false`, `stop_requested_at IS NULL`, not already active/in-flight) through the SAME `_maybe_auto_restart` (halt-gate-FIRST, RestartPolicy ceiling via `_RestartCarry`, Phase-A `uq_live_node_processes_active_deployment` unique index, `max-concurrent-respawn=1` per `gateway_session_key` + jitter/stagger). This closes the WHOLE transient-strand class — incl. the iter-4 no-ACK P1 — regardless of which path failed; correctness no longer depends on per-path retry coverage.
2. **Keep + broaden the fast per-path reaper retry** (`_run_restart_task`): treat `CONCURRENT_STARTUP` + transient post-Phase-A payload-factory NO-ACK `(False, False)` returns as RETRYABLE (not terminal) so the common transient recovers in ~seconds rather than waiting a rescan tick. The retry stays bounded/halt-gated/cancellable; the periodic rescan is the backstop if it gives up.
3. **SPOF (F7) — evaluate-stale-before-publish at boot.** On supervisor start, evaluate the PRIOR `router_heartbeat` (run the stale-router check / one fleet-alert pass) BEFORE `_router_heartbeat_loop` publishes its first fresh stamp — so a restart-after-outage fires `router_spof` for the gap instead of self-masking. Document that a TRUE full-supervisor-outage detector still needs an out-of-process external watcher (deferred to a later PR — NOT built here).
4. **Invariants preserved (blocking):** rescan calls the SAME gated restart path (no bypass of halt/pause/`stop_requested_at`/ceiling/Phase-A/unique-index); Redis halt errors fail closed; the loop is ceiling-bounded (can't manufacture crash-loop churn); non-stampeding (one respawn per shared gateway per pass + jitter); dedup-safe (in-flight dedupe additive; Phase-A + unique index authoritative); the reaper stays non-blocking (backoff in detached tasks).

**Council #4 failing tests (the 7 required):** (1) reaper no-ACK `CONCURRENT_STARTUP` + transient payload-factory → retried on the fast path; (2) a stranded `failed`+eligible deployment → re-driven by the periodic rescan within one interval; (3) fleet-halt / account-halt / `auto_restart_paused=true` / `stop_requested_at` each SUPPRESS rescan recovery; (4) RestartPolicy ceiling blocks repeated reconciliation churn (PAUSED, no respawn); (5) shared-gateway recovery does NOT stampede when multiple accounts fail together (one respawn/gateway/pass); (6) startup rescan behavior preserved through the immediate-first periodic pass (the one-shot is gone, the loop covers boot); (7) `router_spof` evaluates the stale heartbeat BEFORE the first publish after restart (fires for the gap).

- [ ] Failing tests: non-halted crashed node → auto-restarts; account-halted node → NOT restarted; **FLEET-halted (kill-all) crashed node → NOT restarted even though its `account_halt_key` is unset (fleet-OR-account gate — Codex iter-8)**; halt (fleet OR account) arriving during backoff → restart abandoned (cancellable backoff); reconciliation timeout → terminal `RECONCILIATION_FAILED`, no orders; startup re-scan restarts a node that died while the supervisor was down, halt-gated (fleet OR account); re-scan + PEL-recovery racing the same deployment → exactly one restart (unique index), no double-count.
- [ ] **Council #3 reaper-fix failing tests (the P1):** (1) **common crash — node-process row ALREADY `status='failed'` (subprocess wrote it last) → `_on_child_exit(exit_code=1)` STILL auto-restarts** (the reproduced bug: a 2nd node row + deployment back to `starting`); (2) graceful `/stop` (exit 0 → `stopped`) → NOT restarted; (3) **`/stop`-then-crash: `stop_requested_at` set, then non-zero exit → `failed` → NOT restarted** (operator-stop durable through the terminal write); (4) **coincident `/stop` + self-crash serialized** — reaper classify under `FOR UPDATE` either sees committed `stop_requested_at` (suppress) or blocks until `/stop` commits it; (5) halt (fleet OR account) still suppresses the now-terminal-row path; (6) **duplicate reaper invocation on the same `failed` row → `restart_dispatched_at` prevents a second dispatch** (no double-count, no double-spawn); (7) terminal-write-LAST ordering unchanged (subprocess still sole terminal writer; reaper does not pre-empt it); (8) every reaper decision emits its structured log line (incl. `auto_restart_skipped_no_row`).
- [ ] Implement (council #3 hybrid Option A): additive migration (`stop_requested_at` + `restart_dispatched_at`, chained after `d4e5f6a7b8c9`) + model fields; `/stop` lock→set-intent→commit→signal; `_on_child_exit` lock→classify-terminal→suppress-on-intent/halt→set-sentinel→`_maybe_auto_restart`; per-decision logging. Green.

### Task 7: `os.setsid` + `init:true` (node-crash child reaping) [all advisors; honest scope]

**Files:** the node child entrypoint (`trading_node_subprocess.py` `run_subprocess_async` / the `mp.Process` target), `docker-compose.dev.yml` + `docker-compose.prod.yml` (`init: true` on `live-supervisor`); Test `backend/tests/integration/test_node_child_reaping.py`.

**Implementation note (Codex iter-2 P2):** `multiprocessing.Process` does NOT expose `preexec_fn`. Call `os.setsid()` as the FIRST line of the child entrypoint (inside the spawned target function, before the node builds), not via a `preexec_fn` kwarg on `self._spawn_ctx.Process(...)`. `os.setsid()` puts the child in its own session/process group so a SIGTERM to the supervisor's group doesn't cascade to nodes and the child reparents cleanly to the container init on the supervisor's exit. `init: true` makes the container PID-1 init (tini) reap exited node children (no zombie accumulation; tini does the reaping — `os.setsid` only detaches the session). **Scope note (documented in the test + plan):** this hardens the NODE-crash + child-reaping case; it does NOT make a supervisor-PROCESS crash survivable (the supervisor IS PID 1 → its crash recreates the container) — that's the deferred per-account-container capability.

- [ ] Failing test (integration): a node child that exits is reaped (no zombie) under `init:true`; `os.setsid` session is set in the child (assert `os.getsid(child_pid) == child_pid`).
- [ ] Implement (setsid in child entrypoint) + compose change. Green + commit.

### Task 8: US-3 restart-authority health on /live/status + CLI + UI [Hawk/Pragmatist]

**Files:** `api/live.py`, `schemas/live.py`, `cli.py`, `frontend/src/components/live/strategy-status.tsx`, `frontend/src/lib/api.ts`; Test `backend/tests/integration/api/test_live_status_restart_authority.py`.

Per-account fields: `auto_restart_paused`(+reason), `consecutive_respawn_failures`, `last_restart_at`, `last_heartbeat_at` age, halt-latch state. Top-level `router_heartbeat_age_s`. CLI + UI (additive column, `data-testid`).

- [ ] Failing test: `/live/status` carries the fields + `router_heartbeat_age_s`; persists across re-request. Implement API+CLI+UI. Green + commit.

### Task 9: Mandatory alerts [Hawk/Pragmatist — flat-and-unmonitored detector]

**Files:** alerting surface; Test `backend/tests/unit/services/test_pr2_alerts.py`.

(a) `router_heartbeat_age_s > 30` → SPOF alert. (b) Any deployment in `failed` with `last_heartbeat_at > 60s` stale AND no successful respawn → flat-and-unmonitored alert (the single most important alert).

- [ ] Failing tests: stale router heartbeat fires; failed+stale+no-respawn fires; healthy fleet fires neither. Implement. Green + commit.

### Task 10: Account-scoped logging [Maintainer]

**Files:** `fleet_router.py` (post-T5 rename of `process_manager.py`), `main.py` supervision log sites; Test via `capture_logs` (set `ENVIRONMENT=test` in conftest — memory `feedback_structlog_cache_test_pollution`).

Every supervision log/error: account_id, deployment id, pid-if-known, DB status, heartbeat age, halt-latch state, restart decision.

- [ ] Failing test: a restart-decision log carries all fields. Implement. Green + commit.

### Task 11: Binding deploy-contract docs + active-live-gate verification [Contrarian blocking]

**Files:** `docs/how_to_deploy.md` (or deploy runbook) + the release checklist; Test/verify the active-live gate exists in `deploy.yml`.

Document the binding contract: (1) routine deploys exclude the `broker` profile (`deploy-on-vm.sh` `DEFAULT_PROFILE_SERVICES`, F4); (2) the deploy workflow refuses while any live deployment is active (`deploy.yml:399`, F5); (3) broker-profile image changes are deliberate, sequenceable maintenance. Add a release-checklist line: "broker-profile supervisor-image change → sequence as maintenance (drain or accept all-account restart); routine deploys never touch broker." (Decision-doc addendum already written.)

**Coordinated-deploy requirement (PR 2 supervisor↔API contract change).** Because T4 version-couples the API and the supervisor (per-account command streams `msai:live:commands:<account>` + the `router_heartbeat` gate on `/start-portfolio`) and F4 does NOT recreate `live-supervisor` on a routine deploy, deploying PR 2 is a **coordinated release**: after the routine backend deploy the operator MUST recreate the broker profile with the new image (`COMPOSE_PROFILES=broker docker compose -f docker-compose.prod.yml up -d live-supervisor ib-gateway`) BEFORE resuming live trading. Until then `/start-portfolio` returns 503 ("live-supervisor is not running…", `api/live.py:937`) — the intended fail-loud guard. F5 (no deploy while any live deployment is active) guarantees no in-flight command is stranded during the window. **`docs/how_to_deploy.md` is the authoritative operator doc for this** — see its §"Coordinated release: supervisor↔API command-routing/heartbeat contract changes".

**T2 rollout pre-flight (Claude iter-4 P1 + Codex iter-8 MRO).** Add a PR-2 release-checklist line: **before deploying PR 2, every live/queued strategy file MUST declare `class X(RiskAwareStrategy, Strategy)` — the mixin FIRST** so the halt-gated `submit_order` override wins the MRO. The `on_post_build` gate (T2) fails-closed (`SPAWN_FAILED_PERMANENT`) any live strategy that is bare-`Strategy` OR that mixes the order wrong (mixin last → un-gated `Strategy.submit_order` wins), on its next (re)start. The two example strategies are updated in T2; any operator custom strategy in `strategies/` must be migrated too (mixin first), or that account will not start until its code is updated. (One-time migration, not a recurring constraint.) **Test-only fixtures (e.g. `strategies/intentionally_failing_strategy.py`) are intentionally NOT migrated — they are never deployed live, and the gate correctly rejects them with `SPAWN_FAILED_PERMANENT` if someone tries (Claude iter-9 P3).**

- [ ] Confirm the active-live gate (F5) is present + correct in `deploy.yml`; if any gap, fix it (NO BUGS LEFT BEHIND). Write the runbook + release-checklist language (incl. the T2 RiskAwareStrategy pre-flight). Commit.

### Task 12: Supervisor-outage safety harness [Contrarian blocking — the Missing Evidence; test counterpart to US-4]

**Files:** Test `backend/tests/integration/test_supervisor_outage_safety.py`.

With the supervisor (FleetRouter loop) stopped, assert for a running detached node: the **node-side live halt** (T2) blocks new opening orders (node re-checks Redis directly — the key proof the F6 fix delivers router-independent halt); **a `market_exit()` / reduce-only flatten under an active halt is NOT blocked (Claude iter-5 P2 — empirically pins the cpdef-internal-dispatch question and proves the kill-switch can still flatten under halt)**; `/drain` + `/kill-all` still set their Redis latches (API-side, supervisor-independent); the node's own heartbeat thread still updates its row. On supervisor return, the startup re-scan (T6) recovers dead deployments.

**Scope correction (Codex iter-2 P2):** do NOT assert flatness reporting works during a supervisor outage — the flatness `stop_report` is written by the child only AFTER the supervisor pushes the flatness ticket + SIGTERM (`main.py:87`, `trading_node_subprocess.py:1180`). With the supervisor down, no ticket is delivered, so flatness reporting is legitimately unavailable until the supervisor returns. The router-independent safety guarantees PR 2 proves during an outage are: (1) node-side halt blocks new orders, (2) drain/kill-all latches are set, (3) heartbeat rows keep updating. Flatness + SIGTERM-stop require the supervisor (documented as a known outage-window limitation, mitigated by the node-side halt blocking NEW orders).

- [ ] Integration harness asserting the router-independent guarantees during a simulated supervisor outage — (1) node-side halt blocks NEW opening orders, (2) `market_exit()`/reduce-only flatten is ALLOWED under halt, (3) drain/kill-all latches are set, (4) heartbeat rows keep updating (NOT flatness reporting). Green + commit.

### Task 13: Decision-doc migration trigger cross-link [Pragmatist]

**Files:** `docs/decisions/multi-account-broker-fleet.md` (addendum already written — verify the Opt-1 migration trigger is present), plan cross-reference.

- [ ] Verify the council #2 addendum's Shape-A/per-account-container migration trigger (2-VM split OR > 4-5 accounts OR redeploy incident) is present + clear. Commit any tidy-up.

---

## Dispatch Plan

> **Path note:** T5 renames `process_manager.py` → `fleet_router.py`. The tasks after T5 that write the renamed file (**T6, T10**) use `fleet_router.py`, NOT `process_manager.py`. (T7 writes `trading_node_subprocess.py` + compose, not `fleet_router.py`.) The Writes column below uses the post-rename path for T6/T10.

| Task ID | Depends on  | Writes                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1      | —           | `models/live_node_process.py`, `alembic/versions/2026_05_31_*`, `tests/integration/test_pr2_migration.py`                                                                                                                                                                                                                                                                                                 |
| T2      | —           | `services/nautilus/risk/risk_aware_strategy.py`, `services/nautilus/trading_node_subprocess.py` (`on_post_build` `isinstance` enforce + collaborator wiring), `strategies/example/{smoke_market_order,ema_cross}.py`, `CLAUDE.md` (4-layer kill-all wording), `tests/unit/services/nautilus/{risk/test_node_side_live_halt,test_live_strategy_halt_enforcement}.py` (the P0 — no deps, start immediately) |
| T11     | —           | `docs/how_to_deploy.md`, release checklist (doc-only, parallel-safe)                                                                                                                                                                                                                                                                                                                                      |
| T13     | —           | `docs/decisions/multi-account-broker-fleet.md` (doc-only, parallel-safe)                                                                                                                                                                                                                                                                                                                                  |
| T3      | T1          | `live_supervisor/restart_policy.py`, `tests/unit/live_supervisor/test_restart_policy.py`                                                                                                                                                                                                                                                                                                                  |
| T5      | —           | `process_manager.py`→`fleet_router.py` rename + all importers (serialize BEFORE T4/T6/T7/T10)                                                                                                                                                                                                                                                                                                             |
| T7      | T5, T2      | `services/nautilus/trading_node_subprocess.py` (setsid in child entrypoint), `docker-compose.dev.yml`, `docker-compose.prod.yml`, `tests/integration/test_node_child_reaping.py` (serialized after T2 — BOTH write `trading_node_subprocess.py`)                                                                                                                                                          |
| T4      | T5          | `services/live_command_bus.py`, `services/live/flatness_service.py`, `live_supervisor/main.py`, `api/live.py`, tests (shares NO file with T7 → may run concurrently with T7; serialized with T6 on `main.py`; with T8 on `api/live.py`). Single atomic commit: retire the global consumer + switch `_supervisor_is_alive` to `router_heartbeat` together.                                                 |
| T6      | T1,T3,T4,T5 | `fleet_router.py`, `live_supervisor/main.py`, `__main__.py`, `tests/unit/live_supervisor/{test_auto_restart,test_startup_rescan}.py` (serialized after T4 — both write `main.py`)                                                                                                                                                                                                                         |
| T10     | T5,T6       | `fleet_router.py`, `live_supervisor/main.py` (serialized after T6 — same files)                                                                                                                                                                                                                                                                                                                           |
| T8      | T1,T4,T6    | `api/live.py`, `schemas/live.py`, `cli.py`, frontend files, `tests/integration/api/test_live_status_restart_authority.py` (serialized after T4 — both write `api/live.py`)                                                                                                                                                                                                                                |
| T9      | T1          | `services/alerting.py` (or equivalent), `tests/unit/services/test_pr2_alerts.py`                                                                                                                                                                                                                                                                                                                          |
| T12     | T2,T4,T6,T7 | `tests/integration/test_supervisor_outage_safety.py`                                                                                                                                                                                                                                                                                                                                                      |

**Sequencing (Codex iter-2 P2 + iter-3/iter-4 collision resolution):** T1, T2 (the P0), T11, T13 start immediately (disjoint). **T2 writes `trading_node_subprocess.py` (`on_post_build` enforcement + collaborator wiring); T7 also writes `trading_node_subprocess.py` (setsid in the child entrypoint) — so T7 is gated on BOTH T5 and T2** (the dependency is explicit so a slow T2 can't race T7). After T5 (rename), the supervisor-core writes serialize by SHARED FILE, not as one rigid chain:

- `trading_node_subprocess.py`: T2 → T7 (T7 after T2+T5).
- `main.py`: T4 → T6 → T10 (each after the prior).
- `api/live.py`: T4 → T8 (T8 after T4).
- `fleet_router.py`: T5 → T6 → T10.

**T4 and T7 share NO file, so they may run concurrently** after their respective deps clear (this un-blocks the `_supervisor_is_alive`/producer-migration live-safety fix from waiting on the setsid work — Claude iter-4 P2). T3 + T9 (disjoint) run alongside. T12 (integration harness) last. No two concurrently-dispatched tasks share a Writes path.

---

#### E2E Use Cases

> Surfaces (CLAUDE.md fullstack + CLI): API + UI + CLI. **Surface coverage decision:**
>
> - **API: Covered** — UC-API-1 (per-account health), UC-API-2 (drain isolation, sanctioned).
> - **CLI: Covered** — UC-CLI-1 (`msai live status` health).
> - **UI: Covered** — UC-UI-1 (`/live-trading` health column).
> - **Process-kill UCs (node-side halt during outage, auto-restart, kill-A-node isolation): operator-run + integration** — UC-DRILL-1 + T12 harness. Killing a process / stopping the supervisor is NOT a sanctioned E2E interface, so these are covered by the operator drill (UC-DRILL-1) and the T12 integration harness, NOT standard verify-e2e UCs.

**UC-API-1 — Operator reads per-account supervisor health**

- **Actor:** Operator monitoring the live fleet via the API.
- **Scenario:** After bringing the fleet up, the operator checks each account's restart-authority health to confirm nothing is in a paused/failed state before stepping away.
- **Interface:** API.
- **Intent:** The operator confirms fleet health and can spot an account that needs attention.
- **Setup:** Two paper deployments live via `POST /api/v1/live/start-portfolio`; auth `X-API-Key`.
- **Steps:** `GET /api/v1/live/status` → read each deployment's restart-authority fields + top-level `router_heartbeat_age_s` → pick one account and `GET /api/v1/live/status/{deployment_id}` to drill into its detail.
- **Verification:** The operator sees each account row list `auto_restart_paused`(+reason), `consecutive_respawn_failures`, `last_restart_at`, heartbeat age, halt state; a small `router_heartbeat_age_s` confirms the supervisor is alive; the per-deployment detail GET returns the same account's restart-authority fields, so an account with `consecutive_respawn_failures > 0` is identifiable and drillable.
- **Persistence:** Re-request after a short delay → fields still present, `router_heartbeat_age_s` still small (not a one-shot in-memory flag).

**UC-API-2 — Drain isolation + latch persistence (sanctioned, no process kill)**

- **Actor:** Operator draining one account while the other keeps trading.
- **Scenario:** The operator needs to pause account A for review while account B keeps trading, and wants A's drain to survive a resume→drain cycle so the pause is reliable.
- **Interface:** API.
- **Intent:** The operator drains A, sees B unaffected, and confirms A's drain state is durable.
- **Setup:** Two paper deployments (A, B) live via `POST /api/v1/live/start-portfolio`.
- **Steps:** `POST /api/v1/live/drain/{A}` → `GET /api/v1/live/status` → `POST /api/v1/live/resume/{A}` → `POST /api/v1/live/drain/{A}` → `GET /api/v1/live/status`.
- **Verification:** After draining A, status shows A halted + B still `running` with a fresh heartbeat; `GET /api/v1/live/positions` for B is unaffected; after the resume→drain cycle, A's halt latch is set again (durable, account-scoped). B is never affected by A's drain/resume.
- **Persistence:** Re-request `/live/status` after a short delay → A still drained, B still running.

**UC-CLI-1 — Operator reads supervisor health from the CLI**

- **Actor:** Operator on the VM shell.
- **Scenario:** SSH'd in, the operator wants per-account health without the UI.
- **Interface:** CLI. **Intent:** list fleet supervisor health from the shell.
- **Setup:** One paper deployment live via `POST /api/v1/live/start-portfolio`.
- **Steps:** Run `msai live status` → read the per-account rows.
- **Verification:** stdout shows each account with `auto_restart_paused`, `consecutive_respawn_failures`, heartbeat age, and a router-health line; exit 0.
- **Persistence:** Re-run in a new invocation → same health rows (reads live DB+Redis).

**UC-UI-1 — Operator sees supervisor health on the dashboard**

- **Actor:** Signed-in operator on the live-trading dashboard.
- **Scenario:** Opens the live page to glance at fleet health.
- **Interface:** UI. **Intent:** see per-account supervisor health on the existing page.
- **Setup:** One paper deployment live; authenticated UI session.
- **Steps:** Navigate to `/live-trading` → inspect the deployments table.
- **Verification:** Each row shows a supervisor-health cell (healthy / recovering / auto-restart-paused) reading the API field.
- **Persistence:** Reload `/live-trading` → the health cell still renders with the same state.

**UC-DRILL-1 (operator-run) — Kill-A-node + node-side-halt + recovery drill (US-1/US-1b/US-2/US-4)**

- **Actor:** Operator running the live acceptance drill.
- **Scenario:** LVP + HVP both live; operator (1) kills LVP's TradingNode and watches HVP unaffected + LVP auto-restart; (2) stops the supervisor, sets a halt, confirms a running node blocks orders (node-side live halt); (3) re-runs the PR-1 Shape B halt-isolation drill.
- **Interface:** API + process control (operator).
- **Intent:** End-to-end proof of node-crash isolation + node-side halt + recovery before merge.
- **Setup:** LVP + HVP live (Shape B two-gateway compose from PR 1).
- **Steps:** Kill LVP's node process → observe HVP via `/live/status`+`/live/positions` → confirm LVP auto-restarts + reconciles → stop the supervisor → set account halt on a running account → confirm the running node blocks its next order (node-side live halt, T2) → restart supervisor → confirm startup re-scan recovers → re-run PR-1 Shape B halt drill.
- **Verification:** HVP continuous through LVP's node kill; LVP recovers per US-2; node-side halt blocks orders with the supervisor down; PR-1 Shape B halt drill green.
- **Persistence:** After recovery, `/live/status` shows both healthy; per-account halt latches remain independent.

---

## Self-Review

- **Spec coverage:** US-1 → T5/T6/T7/UC-DRILL-1; US-1b (F6 P0) → T2/T12/UC-DRILL-1; US-2 → T3/T6/T12; US-3 → T8/T9/T10/UC-API-1/UC-CLI-1/UC-UI-1; US-4 → UC-DRILL-1; binding deploy contract → T11. All council #2 conditions mapped.
- **Over-engineering removed:** no SupervisorLease, no generation token, no heavy AccountSupervisor actor (council #2). Restart guard is a small DB-backed policy.
- **Additive-only migration:** T1 nullable/defaulted; `database.md` compliant.
- **No PR-1 regression:** halt latches (T2/T6 gate), /drain + /resume (UC-API-2), GatewayRouter + symbology shim + Shape B topology (untouched; T12 + UC-DRILL-1 re-verify).
- **Honest scope:** supervisor-process-crash = container recreate (NOT isolated) is explicitly documented in US-1 + T7; deploy isolation deferred to 2-VM (T11 + decision doc).
- **Node-side gate scope (iter-5/6):** PR 2's mandatory node-side gate is the **HALT only** (the F6 P0). The position/exposure/daily-loss risk-cap suite (needs `_risk_limits`, no live data path today) is guarded/skipped and deferred to PR 5 — behavior-preserving (the suite was already unreachable for live strategies). The node-side halt is a bounded-staleness (≤2s) FAST layer, honestly scoped in US-1b, backstopped by the supervisor kill-all layers when up.
- **E2E sanctioned:** UC-API-2 no longer requires an operator process-restart (Claude 8-A fixed); process-kill UCs are operator-drill + T12 integration only.
