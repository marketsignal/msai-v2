# PR 1b — Data-Stale Auto-Halt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When any required live Databento feed for any active TradingNode goes stale, auto-halt the fleet via the existing emergency latch with `data_stale` cause attribution; expose per-feed warmth (`GET /api/v1/live/data-health` + `msai live data-health`); make `/resume` fail-closed (live warm re-probe + reconciliation-complete); prove all five failure modes in a CI-runnable simulated harness.

**Architecture:** Per the Approach Comparison below (Contrarian-VALIDATED): freshness is observed AND evaluated **in-node** — a `DataFreshnessActor` records last-event timestamps into an in-process registry, and a `DataStaleMonitor` (the `IBDisconnectHandler` injected-loop shape) evaluates session-aware two-granularity grace and sets `msai:risk:halt` + `halt_cause_key("fleet")` directly in Redis on stale. Nodes publish per-feed freshness to TTL'd Redis keys; the API reads those for `data-health`, metrics, and the `/resume` warm re-probe. Reconciliation-complete is a node-published Redis marker (the engine signal is subprocess-internal). The PR 2/F6 node order gate needs ZERO changes — it already reads the latch.

**Tech Stack:** Python 3.12, NautilusTrader 1.223.0 (installed — do NOT bump), redis.asyncio, exchange_calendars (existing dep), FastAPI, Typer, freezegun (dev). No new dependencies.

---

## Approach Comparison

### Chosen Default

In-node evaluation + direct latch set (mirror `IBDisconnectHandler` + F6 philosophy). Node observes its own feeds (Actor records `bar.ts_event` + arrival time per FeedKey = (dataset, native bar type) — staleness evaluated on EVENT time), an injected-dependency async evaluator applies session-aware grace at two granularities (per-dataset alive / per-required-feed fresh), and on stale sets `msai:risk:halt` + `halt_cause_key()` directly. Nodes publish per-feed freshness to Redis (TTL) for data-health/metrics/resume-probe. Supervisor has no evaluation role; node-death stays covered by the existing supervisor heartbeat monitor.

### Best Credible Alternative

Node publishes raw `last_event_ts` → supervisor evaluates + sets latch (single evaluator, dumb nodes).

### Scoring (fixed axes)

| Axis                  | Default | Alternative |
| --------------------- | ------- | ----------- |
| Complexity            | M       | M           |
| Blast Radius          | M       | M           |
| Reversibility         | M       | M           |
| Time to Validate      | L       | M           |
| User/Correctness Risk | L       | H           |

Alternative's H is structural: supervisor outage ⇒ nobody evaluates ⇒ stalled feed never halts ⇒ blind real-money trading — the exact reason F6 is node-side.

### Cheapest Falsifying Test

"In-node evaluator has Redis/config/clock in the subprocess?" — already confirmed: `IBDisconnectHandler` is wired there today (`trading_node_subprocess.py` ~:989 factory + `disconnect_handler.py`).

## Contrarian Verdict

**VALIDATE** (Codex gpt-5.5, 2026-06-03): "Node-side observation/evaluation is the only freshness source and keeps the real-money halt path supervisor-independent; implement cause attribution with `halt_cause_key()` rather than copying IBDisconnectHandler's stale `:reason/:source` keys." The rider is encoded as Task 5 (cause-key convergence — also fixes the pre-existing `/resume`-doesn't-clear-`:reason`/`:source` bug; No Bugs Left Behind).

---

## Grounding (research: `docs/research/2026-06-03-pr1b-data-stale-auto-halt.md`)

- Nautilus Databento adapter exposes NO connection-state / per-sub `last_event_ts`; `is_connected` stays `True` through silent Rust reconnects (`reconnect_timeout_mins` default 10). All five failure modes reduce to event-silence vs session-aware grace.
- Two granularities are mandatory: per-dataset "alive" (any event on the dataset) vs per-required-FEED "fresh" (interval parsed from the BarType spec — 1-min AND 5-min subscriptions exist; thin names tolerate zero-trade-minute gaps via config grace).
- `HaltCause.DATA_STALE` already reserved (`core/halt_keys.py:25`); `halt_cause_key()` is the canonical cause companion; F6 gate (`risk_aware_strategy.py`) reads `msai:risk:halt` with ≤2s bounded staleness.
- `/resume` today is a pure latch-delete with NO preconditions (`api/live.py:3166-3224`).
- Reconciliation signal: `LiveExecutionEngine.reconcile_execution_state() -> bool` + `_startup_reconciliation_event` — subprocess-internal; the subprocess already observes healthy-reconcile (PR 2 T6 streak-reset, `trading_node_subprocess.py:676-689`).
- Session calendars: `services/trading_calendar.py` (XNYS/CMES via exchange_calendars) + `services/nautilus/market_hours.py` (`MarketHoursService`, registry `trading_hours`) + `exchange_local_today()` (Chicago). Gap: equities pre/post phase + GLBX maintenance window need a thin session-phase helper.
- Metrics: hand-rolled labeled registry (`services/observability/metrics.py` `Gauge.labels(...)`); cardinality bounded to active subscriptions.

**Futures scope honesty (Codex iter-1 P1):** the live per-account Databento topology is **equities-only today** — non-equities raise in `symbology_shim.py:219` and fall back to the legacy builder (`live_supervisor/__main__.py:504`, `trading_node_subprocess.py:2345`). PR 1b therefore ships futures freshness as **config + sessions + simulated-harness coverage** (GLBX grace budgets, CMES session phases incl. the maintenance window, and the harness's GLBX failure modes — all fully implemented and tested in simulation), so the day a futures node ships it is protected from day one — but NO live futures feed exists to observe in this PR, and PR 1b does NOT build the futures Databento bridge (that belongs to the PR that ships futures trading, per the ratified slicing). The runtime observation universe in PR 1b = whatever feeds the node's actual config wires (equities today). This satisfies Pablo's "Equities + ES futures" scope decision at the correct altitude: detection LOGIC covers both asset classes; live wiring observes what exists.

## File Structure

```
backend/src/msai/
├── core/halt_keys.py                                  # M: freshness/reconciled key helpers (+ docstring on cause JSON shape)
├── services/live/data_freshness.py                    # A: config model + session-phase resolver + FreshnessRegistry + evaluate()
├── services/nautilus/data_freshness_actor.py          # A: DataFreshnessActor (records event ts into registry)
├── services/nautilus/data_stale_monitor.py            # A: DataStaleMonitor (injected-loop evaluator; latch + cause + publish)
├── services/nautilus/disconnect_handler.py            # M: cause-key convergence (halt_cause_key, drop ad-hoc keys)
├── services/nautilus/trading_node_subprocess.py       # M: wire registry/monitor lifecycle + reconciled marker (run loop)
├── services/nautilus/live_node_config.py              # M: append DataFreshnessActor ImportableActorConfig to actors=[...]
├── services/observability/trading_metrics.py          # M: labeled freshness/health gauges
├── main.py                                            # M: /metrics route awaits hydrate_data_health_metrics() before render
├── api/live.py                                        # M: /resume preconditions + GET /live/data-health
├── schemas/live.py                                    # M: DataHealthResponse / ResumeRefusal schemas
└── cli.py                                             # M: `msai live data-health`
backend/tests/
├── unit/test_data_freshness.py                        # A: config, session phase, evaluate() matrix
├── unit/test_data_stale_monitor.py                    # A: 5 failure modes + negatives (the harness core)
├── unit/test_disconnect_handler_cause_keys.py         # A: convergence regression
├── integration/api/test_live_resume_fail_closed.py    # A: /resume matrix
├── integration/api/test_live_data_health.py           # A: endpoint + metrics shape
└── integration/test_live_cli_data_health.py           # A: CLI
```

---

### Task 1: Freshness domain — config, session phase, registry, evaluator core

**Files:** Create `backend/src/msai/services/live/data_freshness.py`; Test `backend/tests/unit/test_data_freshness.py`.

- [ ] **RED:** tests for:
  - `GraceConfig` (pydantic): **GRACE-ONLY** — per `(asset_class ∈ {equity, futures}, phase ∈ {rth, pre, post, closed, maintenance})` → `grace_s`; defaults: equity rth 90, pre 300, post 300, closed None (never stale); futures rth 120, maintenance None; loadable from env/JSON override (`DATA_FRESHNESS_GRACE_JSON`), invalid JSON → ValueError at load (fail-loud), unknown keys rejected. **`expected_interval_s` is NOT config** — it is parsed per feed from the native `BarType` spec (so 1-MINUTE and 5-MINUTE subscriptions each get their true cadence; a config interval would false-halt 5-min feeds — Codex iter-6 P1). The per-DATASET "alive" budget is deterministic: `min(parsed interval among the dataset's required feeds) + phase grace`. The startup-grace + interval×4 ≤ min-grace assert applies to the grace values only.
  - `resolve_session_phase(asset_class, now_utc) -> Phase` composing `trading_calendar._calendar()` (XNYS/CMES is_session + session_open/session_close) with equities pre/post anchored RELATIVE to the calendar's ACTUAL session bounds — pre = [session_open − 5h30m, session_open), post = [session_close, session_close + 4h) — so EARLY-CLOSE days (13:00 ET half-days, which exchange_calendars encodes in session_close) shift post-market correctly instead of leaving a 13:00–16:00 undefined gap (Claude iter-2 P2); + the GLBX daily maintenance window (16:00–17:00 CT Mon–Thu; weekend closed) — all boundaries computed in exchange-local tz, anchored per `exchange_local_today()` discipline (Chicago for CMES, New York for XNYS). freezegun matrix across each boundary INCLUDING an early-close day (day-after-Thanksgiving 13:00 ET close: 13:30 ET must resolve to `post`, not `closed`/`rth`). Note: CMES sessions are close-date-anchored in exchange_calendars — Sunday-evening Globex reopen resolves to `closed` (never-stale, FAIL-SAFE no-false-halt; acceptable while futures are simulation-only in PR 1b — documented, revisit when live futures ship).
  - **Feed identity is the FULL required subscription, not the symbol** (Codex iter-2 P1): `FeedKey(dataset, native_bar_type_str)` — the same `canonical_to_native_bar_types` values the node wires (5-MINUTE subscriptions exist in repo configs; symbol-keying would false-halt them under a hardcoded 60s). `expected_interval_s` is PARSED from the `BarType` spec (step+aggregation), with the GraceConfig providing the per-(asset_class, phase) GRACE only; `symbol` is a display field derived from the bar type. `FreshnessRegistry`: `record(feed_key, ts_event_ns, ts_arrival_ns)` (thread-safe) + `snapshot() -> dict[FeedKey, FeedObservation]` + derived per-dataset max. **Records BOTH the source event timestamp (`bar.ts_event`) and arrival time** — staleness is evaluated on EVENT time, so a feed delivering bars whose `ts_event` is frozen in the past (US-006 mode 2, stale-timestamp) goes stale even though arrivals continue (Codex iter-1 P1). Arrival time is kept for diagnostics in the published freshness JSON.
  - `evaluate(required_feeds, snapshot, now_ns, monitor_started_ns, cfg, phase_resolver) -> list[StaleFinding] | []` — **the required-feed universe and the monitor start time are explicit inputs** (Codex iter-2 P1): a required feed ABSENT from the snapshot (never observed) is stale once `now − monitor_started_ns > startup_grace_s` (default 180) during an open phase — manifest-minus-snapshot CAN auto-halt, not just block resume. Two granularities: feed stale iff `now − max(last_EVENT_ts, current_phase_open_ts) > (its parsed expected_interval + phase grace)` AND phase is open — the **phase-open clamp** means a node carrying yesterday's `last_event_ts` into pre/RTH (or exiting GLBX maintenance) is NOT halted on the first open tick; the budget restarts at phase open (Codex iter-3 P1). ACCEPTED COST (document in code): a feed dying within one grace window of a phase boundary may not be detected until `phase_open + interval + grace` of the NEXT open phase — bounded to ≤1 grace window per boundary (≤3 equity boundaries/day), safe because once latched no phase rollover can un-halt (operator-only resume), and the alternative (carrying a was-stale latch across phases) would reintroduce the reopen false-halt. Test: the reopen no-halt path explicitly does NOT carry staleness across the boundary. The phase resolver therefore returns `(phase, phase_open_ts)`. Dataset stale iff max-over-feeds' CLAMPED event ts exceeds the dataset budget. `StaleFinding(dataset, feed_key|None, symbol|None, last_event_ts|None, detected_at, granularity)`. CLOSED/maintenance phases never stale. Monitor tick interval MUST be ≪ the smallest configured grace (assert at config load: `interval_s * 4 <= min(open-phase grace_s)`) so the tick can never swallow a budget.
- [ ] **GREEN:** implement; `uv run --extra dev python -m pytest tests/unit/test_data_freshness.py -q` passes.
- [ ] ruff + mypy --strict clean.

### Task 2: DataFreshnessActor — in-node event observation

**Files:** Create `backend/src/msai/services/nautilus/data_freshness_actor.py`; Modify `backend/src/msai/services/nautilus/live_node_config.py` (actor wiring — `build_per_account_trading_node_config` appends to `actors=[...]` at ~:1085, mirroring the `SymbologyShimActor` `ImportableActorConfig` assembly at ~:1043-1053); Test added to `backend/tests/unit/test_data_freshness.py` (actor section).

- [ ] **RED:** Actor (subclass `nautilus_trader.common.actor.Actor`; `ActorConfig` subclass with PRIMITIVES ONLY — the kernel instantiates it from a serialized `ImportableActorConfig`) configured with the node's required native bar types + the per-instrument dataset map (thread `canonical_to_native_bar_types` + `venue_dataset_map` from the existing `TradingNodePayload` fields ~:462-471 — do NOT re-derive ad hoc); `on_start` subscribes to those bar types (msgbus fan-out — VERIFIED no duplicate upstream Databento subscription: `engine.pyx:1191` only calls `client.subscribe_bars` when the bar type isn't already subscribed); `on_bar` calls `FreshnessRegistry.record(feed_key, bar.ts_event, clock.timestamp_ns())` (FeedKey = dataset + native bar-type string) — **event time from `bar.ts_event`** (Codex iter-1 P1). The shared `FreshnessRegistry` CANNOT cross the primitives-only config boundary: inject it POST-`node.build()` via a `set_registry(...)` setter. Retrieval of the built instance: `node.trader.actors()` (verified `trading/trader.py:150-159` in installed nautilus) filtered by component/type, called from the subprocess's existing post-build wiring point (the `on_post_build` hook / `_wire_market_hours` precedent at `trading_node_subprocess.py:1183-1184,1849` — the REAL production seam; note `SymbologyShimActor.set_audit_sink` defines the setter API shape but is wired only in tests, so do NOT cite it as the runtime precedent — Claude iter-2 P1). Tests: stub registry + fabricated bars asserting ts_event recorded; a registry-INJECTION test (actor without injected registry no-ops safely + logs once, never crashes the node); config round-trips through `ImportableActorConfig` serialization.
- [ ] **GREEN** + ruff/mypy.

### Task 3: DataStaleMonitor — evaluator loop, latch, cause, publish

**Files:** Create `backend/src/msai/services/nautilus/data_stale_monitor.py`; Test `backend/tests/unit/test_data_stale_monitor.py` (THE multi-failure-mode harness core).

- [ ] **RED:** `DataStaleMonitor(registry, required_feeds, redis_factory, cfg, phase_resolver, deployment_id, account_id, node_id, clock=...)` — `required_feeds: set[FeedKey]` from the actor config (the manifest source); `node_id` maps from the payload's `deployment_slug`/`row_id` (no literal node_id field exists — P3) — injected-loop shape copied from `IBDisconnectHandler` (start/stop task, interval default 5s, never raises out of the loop):
  - at START: publish the deployment's **required-feed MANIFEST** to `data_freshness_manifest_key(deployment_id)` (new helper; the SET of FeedKeys (dataset + native bar type, with display symbol) from the actor config — the exact universe this node requires; TTL'd + re-set per tick as specified below; **freshness-INELIGIBLE nodes (legacy/non-Databento fallback — the `symbology_shim.py:219` NotImplementedError path routing to the legacy builders `build_portfolio_trading_node_config`/`build_live_trading_node_config` at `live_node_config.py:632`/`:376`, where `canonical_to_native_bar_types` is empty) still run the monitor and publish an EMPTY manifest**, meaning 'no Databento feeds required' — distinct from 'monitor never started' (absent manifest), so `/resume` treats empty-manifest deployments as vacuously warm instead of wedging legacy nodes on the no-manifest 409 (Codex iter-4 P1); TTL'd (below), DELETEd with the per-feed keys at clean monitor stop). The manifest is the source of truth `/resume` + `data-health` compare against, so a feed that NEVER publishes a freshness row is VISIBLE as missing rather than silently absent (Codex iter-1 P1);
  - each tick: `evaluate(...)`; publish per-feed freshness JSON to **`data_freshness_key(deployment_id, dataset, native_bar_type)`** (new helper in `core/halt_keys.py`; the FULL native bar-type string is the key segment — two intervals on the same symbol are DISTINCT keys, Codex iter-3 P1; value JSON: `{last_event_ts, last_arrival_ts, verdict, phase, grace_s, account_id, node_id, symbol, published_at}` (`published_at` = monitor tick wall-clock — the API surfaces it verbatim; do NOT infer from TTL; Codex iter-4 P1), TTL = 3× interval) PLUS a **plain-string verdict companion** `data_freshness_key(...) + ":verdict"` = `"warm"|"stale"` (same TTL; the monitor publishes ONLY warm/stale — `"missing"` is derived EXCLUSIVELY by the API for a manifest feed with no row, so the field has a single producer per value; Claude iter-4 P2) so `RESUME_CLEAR_LUA` does a bare `GET == 'warm'` with NO cjson dependency (Claude iter-3 P2 — zero Lua-JSON precedent in the repo); the MANIFEST key is SET with TTL = 3× monitor-tick-interval at START and RE-SET with the same TTL EVERY tick (the initial publish carries the TTL too — a kill before the first tick must not leave an immortal orphan; Codex iter-4 P2; TTL multiplier is of the 5s MONITOR tick, not the bar interval — disambiguation per Claude iter-4 P3) (a SIGKILLed node's manifest self-expires → /resume then takes the documented "no manifest → 409 monitor-not-started" path instead of wedging on a stale orphan; Claude iter-3 P2) — published EVERY tick (warm and stale) so the API's re-probe sees liveness of the monitor itself; encoding: plain JSON strings — the monitor opens its OWN aioredis client (`decode_responses=False`, mirroring `_real_disconnect_handler_factory` ~:1783) and the API reads via the TEXT bus client `bus._redis` (`decode_responses=True`, `live_deps.py:66-76`) — both directions are JSON-safe; state this in the module docstring so nobody double-decodes;
  - on first `StaleFinding`: write the halt via a NEW shared **atomic halt-write Lua script** (constant in `core/halt_keys.py`, e.g. `HALT_WRITE_LUA`): in ONE atomic script — SET latch (TTL 24h) + SET `:set_by`/`:set_at` + SET cause ONLY-IF-ABSENT + LPUSH cause JSON to `halt_cause_key("fleet")+":history"` + LTRIM 50. Atomicity preserves BOTH the F9 all-or-nothing guarantee AND preserve-existing-cause semantics (plain SETNX inside a MULTI/EXEC pipeline cannot read-then-decide — Claude iter-2 P1). Retry/backoff around the script call mirrors `IBDisconnectHandler` (`disconnect_handler.py:85,220`). Cause JSON: `{reason: HaltCause.DATA_STALE, account_id, node_id, deployment_id, dataset, feed, symbol, detected_at, last_event_ts}`;
  - idempotent while stale (no re-fire spam; one alert metric inc per transition); resumes publishing warm verdicts after recovery WITHOUT clearing the latch (operator-only resume).
  - **Failure-mode matrix (all with injected registry snapshots + frozen clock; no sockets):** (1) disconnect = all feeds silent past dataset grace → halt, dataset attribution; (2) stale-timestamp = events arriving but `last_event_ts` frozen in past → halt; (3) partial-dataset stall = EQUS.MINI silent, GLBX flowing → halt, stalled-dataset attribution; (4) single-symbol stall = one symbol stale, dataset alive → halt, symbol attribution; (5) reconnect-storm = repeated short gaps each recovering within grace → NO halt; PLUS negatives: closing-bar natural gap (≤ expected+grace) no-halt; closed/maintenance phase no-halt; **session-REOPEN no-halt** (overnight node entering pre/RTH with yesterday's last_event_ts → warm until phase_open + interval + grace; same for GLBX maintenance-exit); `is_connected`-irrelevance (no flag consulted); existing-manual-cause not overwritten.
- [ ] **GREEN** + ruff/mypy.

### Task 4: Node wiring — actor + monitor + reconciled marker

**Files:** Modify `backend/src/msai/services/nautilus/trading_node_subprocess.py`, `backend/src/msai/services/nautilus/live_node_config.py` (actor list — see Task 2), `backend/src/msai/core/halt_keys.py`; Test `backend/tests/unit/test_data_freshness.py` (wiring section) + extend existing subprocess tests.

- [ ] **RED:** (a) payload gains `data_freshness_enabled: bool = True` + optional grace-override JSON (primitives only — payload contract); (b) in the subprocess **run loop**: build the shared registry, post-`build()` inject it into the actor (Task 2 setter), start `DataStaleMonitor` after node start, stop it in the finally (same lifecycle discipline as `IBDisconnectHandler`); (c) NEW `reconciled_key(deployment_id)` helper in `halt_keys.py`; the marker is DELETEd at subprocess start (run-loop level, before node start — restart re-arms fail-closed) and SET (ISO-now) **in the run loop immediately after `await _mark_running(...)` succeeds (~:1268-1269)** — NOT inside `_mark_running` itself, which is a DB-only helper with no Redis/node handle (Claude iter-1 P1). Rationale for why this point IS the reconcile-boolean signal (bank it in the code comment): in installed nautilus 1.223.0, `kernel.py:1025-1037` returns early on failed/timed-out reconciliation so `trader.start()` is never reached → `trader.is_running` stays False → `wait_until_ready` (which gates `_mark_running`) cannot pass without a healthy reconcile. Do NOT read `ExecutionEngine.reconciliation` (that property is the config FLAG, not completion — `execution_engine.py:260`). Tests: marker DELETEd at start; marker SET only after `_mark_running`; marker absent when readiness fails.
- [ ] **GREEN** + ruff/mypy. Monitor inert in backtests (never wired there — same rule as the F6 gate).

### Task 5: Cause-key convergence (pre-existing bug fix; Contrarian rider)

**Files:** Modify `backend/src/msai/services/nautilus/disconnect_handler.py`, `backend/src/msai/api/live.py` (/resume + /kill-all cause writes if needed); Test `backend/tests/unit/test_disconnect_handler_cause_keys.py`.

- [ ] **RED:** (a) `IBDisconnectHandler._fire_halt()` writes `halt_cause_key("fleet")` canonical JSON (`reason=HaltCause` value for IB disconnect — add an `IB_DISCONNECT` member if none fits; verify against the enum first) with the SAME SETNX + history-append semantics as Task 3, instead of ad-hoc `msai:risk:halt:reason`/`:source`; (b) **`/kill-all` (api/live.py:2664-2681) replaces its MULTI/EXEC halt-write with the SAME shared `HALT_WRITE_LUA` script** (preserve-existing cause + history-append, atomic — today its unconditional in-pipeline `SET halt_cause_key` would silently erase a data-stale attribution, and naive SETNX inside the pipeline can't read-then-decide; Codex iter-1 P1 + Claude iter-2 P1) — NOTE: the /kill-all + /resume live.py edits EXECUTE in Task 6 (single live.py owner; this task owns disconnect_handler.py + the Lua/keys only); (c) `/resume` clears `halt_cause_key("fleet")`, the `:history` list, AND (transition compat) the legacy `:reason`/`:source` keys; regression tests: after IB-disconnect halt + `/resume`, NO `msai:risk:halt:*` residue remains; kill-all atop an existing data-stale cause preserves the original cause with the manual cause in history.
- [ ] **GREEN** + ruff/mypy.

### Task 6: Fail-closed `/resume`

**Files:** Modify `backend/src/msai/api/live.py` (the `/resume` route, `api/live.py:3166`), `backend/src/msai/schemas/live.py`; Test `backend/tests/integration/api/test_live_resume_fail_closed.py`.

- [ ] **RED:** before the existing latch-delete, `/resume` now: (1) loads ACTIVE deployments using the canonical **`ACTIVE_DEPLOYMENT_STATUSES` imported from `broker_account_service` (:72 — the 5-tuple INCLUDING `stopping`)**, not a re-hardcoded 4-tuple (Claude iter-1 P2: a `stopping` deployment still holds IB positions, gotcha #13, and its feed may still be required — and the codebase must not grow a 4th copy of this set); (2) for each, reads `data_freshness_manifest_key(deployment_id)` then **compares manifest vs present `data_freshness_key(...)` rows — a manifest feed with NO row (never published / TTL-expired) is BLOCKING**, and every present row must be `verdict == warm`; absent/expired/stale → 409 `RESUME_BLOCKED_DATA_STALE` naming the feed(s) (Codex iter-1 P1 manifest fix); a deployment with NO manifest at all → 409 fail-closed (monitor never started); a deployment with an EMPTY manifest → vacuously warm for the freshness check (legacy/non-Databento node — no feeds required; reconciled marker still required); (3) every active deployment must have `reconciled_key(...)` present → else 409 `RESUME_BLOCKED_RECONCILIATION`; (4) zero active deployments → vacuous pass (nothing can trade); (5) the success-path CLEAR is a NEW **atomic check-and-clear Lua script** (`RESUME_CLEAR_LUA` in `core/halt_keys.py`): given the full freshness/reconciled key list (built in Python from the manifests), the script atomically re-verifies (a) EVERY active deployment's MANIFEST key still EXISTS — including EMPTY manifests — so a monitor that died between the Python probe and the clear (manifest TTL lapsed) aborts the resume (monitor-death race; Codex iter-5 P1), (b) EVERY feed's plain-string `:verdict` companion EXISTS and equals `"warm"` (bare `GET` compare — no JSON parsing in Lua), AND (c) every reconciled key exists, and ONLY THEN deletes latch + ALL cause keys (incl. `:history` + legacy `:reason`/`:source`); any failed check aborts the clear and returns the offending key → 409. This satisfies the PRD warms-then-re-stales edge case EXACTLY (refuse if a feed re-stales between probe and clear — atomic, no race window; Codex iter-2 P1 spec-loss fix); a feed re-staling AFTER the atomic clear is a NEW stale event the monitor re-halts within one tick. This task ALSO lands the Task-5(b) `/kill-all` switch to `HALT_WRITE_LUA` (live.py single-owner). The success path RETAINS the existing `/resume` `stop_unknown:*` marker scan/delete (`api/live.py:3198` — later `/stop` calls must not keep returning flatness-unknown; Codex iter-5 P2) — add it to the regression matrix. Records who resumed + verified preconditions in the response/log. Reads via the TEXT bus client `bus._redis`. Error envelope per `api-design.md`. Matrix tests: warm+reconciled→200; stale feed→409 naming feed; manifest-feed-with-no-row→409; missing manifest→409; expired freshness key (monitor dead)→409 fail-closed; missing reconciled marker→409; zero-active→200; warms-then-restales (key flipped stale between Python probe and script — script aborts, latch intact → 409); kill-all-atop-data-stale preserves original cause + history.
- [ ] **GREEN** + ruff/mypy.

### Task 7: `GET /api/v1/live/data-health` + schemas + metrics

**Files:** Modify `backend/src/msai/api/live.py`, `backend/src/msai/schemas/live.py`, `backend/src/msai/services/observability/trading_metrics.py`, `backend/src/msai/services/observability/metrics.py` (labeled-child REPLACE/prune support), `backend/src/msai/services/nautilus/trading_node_subprocess.py` (the OrderRejected-branch Redis INCR — serialized AFTER T4's subprocess edits), `backend/src/msai/main.py` (the `/metrics` route at :497 — it is `async def`, so it awaits `hydrate_data_health_metrics(redis)` before `get_registry().render()`); Test `backend/tests/integration/api/test_live_data_health.py`.

- [ ] **RED:** route returns `{feeds: [{account_id, node_id, deployment_id, dataset, feed (native bar type), symbol, last_event_ts, phase, grace_s, verdict, published_at}], fleet_halted, halt_cause}` — built MANIFEST-FIRST per active deployment (same `ACTIVE_DEPLOYMENT_STATUSES` + manifest comparison as Task 6): a manifest feed with no freshness row appears as `verdict: "missing"` (never silently absent); empty fleet → `feeds: []` 200; standard auth; reads via `bus._redis`. On each read, set labeled gauges `DATA_FEED_AGE_SECONDS{account,node,dataset,symbol,feed}` + `DATA_FEED_STALE{...}` (BOTH `symbol` — the PRD US-005 explicit label contract — AND `feed` = native bar type for interval uniqueness; two intervals on one symbol are distinct series; metrics-shape test asserts `symbol` present — Codex iter-3/iter-5 P1) + **`DATABENTO_DATASET_ALIVE{account,node,dataset}`** (per-dataset connection-health derived from dataset-granularity freshness — US-005's "Databento connection-health" series; Codex iter-1 P2), bounded to active manifests; metrics-shape test asserts each label set. **Metrics export path (Codex iter-3 P1 — `/metrics` renders ONLY the API-process registry, `main.py:497`; subprocess counters never reach it):** all PR 1b series are **Redis-hydrated**. The freshness/dataset-alive gauges are set by a shared `hydrate_data_health_metrics(redis)` helper (manifest-driven, same reader as the data-health route) invoked from BOTH the data-health route AND the `/metrics` render path — so a bare Prometheus scrape gets fresh values without anyone calling data-health. `IB_EXEC_PACING_ERRORS{account}`: the node INCRs a plain Redis counter `msai:metrics:ib_exec_pacing:{account_id}` at the NAMED seam — the engine-level audit hook's `OrderRejected` branch (`trading_node_subprocess.py:2148-2150`, subscribed via `events.order.*` at `:2157`), matching IB pacing/throttle codes in `event.reason` — and the API hydrates the counter gauge from that Redis key in the same helper. Gauge hydration REPLACES the labeled children on every hydrate (add a `replace_children`/prune API to `metrics.py` — the registry has no child deletion today (:257), so stopped feeds would linger forever; Codex iter-4 P2). Unit test: fabricated OrderRejected increments the Redis counter; integration: a BARE `/metrics` scrape with NO preceding data-health call exposes the hydrated `DATA_FEED_*`/`DATABENTO_DATASET_ALIVE`/`IB_EXEC_PACING_ERRORS` series (the export-path defect test), and a feed removed from the manifest disappears from the next scrape.
- [ ] **GREEN** + ruff/mypy.

### Task 8: CLI `msai live data-health`

**Files:** Modify `backend/src/msai/cli.py`; Test `backend/tests/integration/test_live_cli_data_health.py`.

- [ ] **RED:** thin HTTP client of the new route (same `_api_call` pattern as `live status`); renders table (account, dataset, symbol, age, phase, verdict) + fleet-halt banner with cause; non-2xx surfaces API error to stderr + exit 1; ANSI-safe help-test discipline (strip SGR — lesson from PR #88 CI).
- [ ] **GREEN** + ruff/mypy.

### Task 9: CHANGELOG + decision-doc addendum

- [ ] `docs/CHANGELOG.md` entry; one-paragraph addendum in `docs/decisions/multi-account-broker-fleet.md` (2026-06-03): PR 1b implemented per blocking objection #9, contrarian-validated in-node shape, cause-key convergence fixed.

---

## Dispatch Plan

| Task ID | Depends on          | Writes                                                                                                                          |
| ------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| T1      | —                   | `services/live/data_freshness.py`, `tests/unit/test_data_freshness.py`                                                          |
| T2 | T1 | `services/nautilus/data_freshness_actor.py`, `services/nautilus/live_node_config.py` (actor wiring — T4 layers on this edit), (append) `tests/unit/test_data_freshness.py` |
| T3      | T1                  | `services/nautilus/data_stale_monitor.py`, `core/halt_keys.py` (freshness/manifest/verdict keys + HALT_WRITE_LUA), `tests/unit/test_data_stale_monitor.py` |
| T4 | T2, T3 | `services/nautilus/trading_node_subprocess.py`, `services/nautilus/live_node_config.py` (serialized AFTER T2's actor wiring), `core/halt_keys.py` (reconciled_key — serialized AFTER T3's halt_keys edit), (append) `tests/unit/test_data_freshness.py` |
| T5      | T4 (halt_keys)      | `services/nautilus/disconnect_handler.py`, `tests/unit/test_disconnect_handler_cause_keys.py` <!-- /kill-all + /resume live.py edits execute in T6 --> |
| T6      | T4, T5              | `api/live.py`, `schemas/live.py`, `tests/integration/api/test_live_resume_fail_closed.py`                                       |
| T7 | T6 (live.py serial) | `api/live.py`, `schemas/live.py`, `services/observability/trading_metrics.py`, `services/observability/metrics.py`, `services/nautilus/trading_node_subprocess.py` (OrderRejected INCR — serialized after T4), `main.py`, `tests/integration/api/test_live_data_health.py` |
| T8      | T7                  | `cli.py`, `tests/integration/test_live_cli_data_health.py`                                                                      |
| T9      | T8                  | `docs/CHANGELOG.md`, `docs/decisions/multi-account-broker-fleet.md`                                                             |

**Sequential mode** — T6/T7 share `api/live.py` + `schemas/live.py`; T4/T5 share halt-key semantics. The only safe parallel pair is (T2, T3) after T1; given the real-money surface, dispatch one at a time.

---

## Developer Briefing

**What I'll build:** If any live market-data feed stops updating, the whole fleet automatically stops opening new positions within seconds, the halt is labeled "data stale — here's exactly which account/feed/symbol and when," operators can check every feed's freshness from the API or CLI, and the resume command refuses to restart trading until the data is verifiably flowing again and the broker state has been reconciled. `[planned]`

**Planned file-map:** see File Structure above. **Key decisions:** in-node detection (survives supervisor outage) `[planned]`; reuse of the existing halt latch + reserved `data_stale` cause `[verified — halt_keys.py:25]`; no Nautilus version bump; fixes the pre-existing cause-key divergence bug in the same branch.

#### E2E Use Cases

**Surface coverage decision:** API — Covered (UC-DS-API-1, UC-DS-API-2). CLI — Covered (UC-DS-CLI-1). UI — N/A: PR 1b is an operator fleet-safety surface (API-first/CLI-second per project ordering); the UI fleet view ships in PR 4 by ratified sequencing — no UI page exists or is added for data-health in this PR.

**UC-DS-API-1 — Operator checks feed warmth on a quiet fleet**

- **Actor:** Fleet operator integrating via the HTTP API
- **Scenario:** No deployments are live yet this session. Before deploying real money, the operator wants to confirm the data-health surface answers (rather than erroring) and reflects the true (empty) feed state, so they can trust it during an incident.
- **Interface:** API
- **Intent:** The operator reads the fleet's data-feed health and gets a trustworthy, well-formed answer even with nothing running.
- **Setup:** Authenticated session (X-API-Key dev auth); ensure no active deployments (`GET /api/v1/live/status` shows none active).
- **Steps:** `GET /api/v1/live/data-health` → inspect body → `GET /api/v1/live/status` (cross-check no actives)
- **Verification:** Receives 200 with `feeds: []`, `fleet_halted` reflecting the real latch state, and no error; the response shape lets the operator script against it (fields documented above).
- **Persistence:** Re-request `GET /api/v1/live/data-health` after a delay — same well-formed empty result (no flapping, no 5xx).

**UC-DS-API-2 — Operator halts the fleet and resumes it safely**

- **Actor:** Fleet operator running an incident drill via the HTTP API
- **Scenario:** The operator drills the emergency path before real money: halt the fleet manually, confirm the halt cause is attributed as a MANUAL halt (not data-stale), then resume and confirm the system verifies preconditions and clears every halt artifact.
- **Interface:** API
- **Intent:** The operator can halt and then resume the fleet, with the resume verifying it is safe and leaving no stale halt residue.
- **Setup:** Authenticated session; no active deployments (vacuous-warm resume path — the stale-refusal path is covered by the in-repo harness since dev has no live feed).
- **Steps:** `POST /api/v1/live/kill-all` → `GET /api/v1/live/data-health` (see `fleet_halted: true` + manual cause, NOT `data_stale`) → `POST /api/v1/live/resume` → `GET /api/v1/live/data-health`
- **Verification:** Resume returns 200 with the verified-preconditions summary; the follow-up data-health read shows `fleet_halted: false` and NO residual halt cause; the cause seen mid-drill was attributed manual (distinguishable from data_stale).
- **Persistence:** Re-request data-health after a delay — still unhalted, still no cause residue (the Task-5 bug fix observable).

**UC-DS-CLI-1 — Operator checks feed warmth from the CLI**

- **Actor:** Operator from the CLI on the host
- **Scenario:** During an incident the operator is in a shell, not a dashboard; they need the same warmth answer the API gives, human-readable.
- **Interface:** CLI
- **Intent:** The operator runs one command and reads per-feed warmth + fleet halt state.
- **Setup:** Authenticated env (`MSAI_API_URL`, `MSAI_API_KEY`); stack running.
- **Steps:** Run `msai live data-health` → (if halted from a prior drill step) read the halt banner → run again after `/resume`
- **Verification:** stdout shows the table (or an explicit "no active feeds" line) and the fleet-halt banner with cause when halted; exit 0; stderr explains any API error with the status code.
- **Persistence:** A fresh shell invocation returns the same state — the command is a faithful read of the live system, not cached.

---

## Self-Review notes

- Spec coverage: US-001→T1-T4; US-002→T3 (SETNX + history) + T5 + UC-DS-API-2; US-003→T7/T8 + UCs; US-004→T4 (marker) + T6; US-005→T7 metrics; US-006→T3 matrix. PRD edge cases each named in a task's test list.
- The stale-REFUSAL `/resume` path and the five failure modes are covered by unit/integration tests (no live feed exists in dev/CI) — E2E covers the operator-journey surfaces; this is the same envelope split ratified for spawn-wiring (#88).
- Types used before defined: none (T1 defines the domain; later tasks import it).
