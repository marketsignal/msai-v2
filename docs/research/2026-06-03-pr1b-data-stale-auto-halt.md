# Research: PR 1b — Data-Stale Auto-Halt

**Date:** 2026-06-03
**Feature:** Per-account/node/feed Databento freshness detection on live NautilusTrader nodes; on stale → set the existing Redis fleet halt latch with `reason=data_stale`; session-aware grace budgets; `GET /api/v1/live/data-health` + `msai live data-health`; fail-closed `/resume`; CI-runnable multi-failure-mode harness with simulated feeds.
**Researcher:** research-first agent

> Grounding discipline: every Nautilus/Databento claim below is cited to the **installed** source under `backend/.venv/lib/python3.12/site-packages/` (nautilus_trader **1.223.0**, databento **0.71.0**), and every internal-plumbing claim to the worktree source. Web sources are used only for the Databento live-gateway heartbeat semantics, which are not fully observable from the wrapped Rust client.

## Libraries Touched

| Library                | Our Version                          | Latest Stable                         | Breaking Changes                                                                             | Source                                                                                                                   |
| ---------------------- | ------------------------------------ | ------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| nautilus_trader[ib]    | 1.223.0 (installed; pin `>=1.222.0`) | ~1.224.x (verify on PyPI before bump) | None adopted — research is against installed 1.223.0; **do not bump in this PR**             | [installed METADATA](backend/.venv/lib/python3.12/site-packages/nautilus_trader-1.223.0.dist-info/METADATA) (2026-06-03) |
| databento (python SDK) | 0.71.0 (installed; pin `>=0.43.0`)   | 0.71.x                                | N/A — adapter wraps the Rust `nautilus_pyo3.DatabentoLiveClient`, not this SDK's live client | [installed METADATA](backend/.venv/lib/python3.12/site-packages/databento-0.71.0.dist-info/METADATA) (2026-06-03)        |
| exchange_calendars     | pinned `>=4.5,<5.0` (already a dep)  | 4.x                                   | None                                                                                         | [pyproject.toml:15](backend/pyproject.toml) (2026-06-03)                                                                 |

No NEW external dependency is required. The session-calendar need (target 4) is already satisfiable with the installed `exchange_calendars` dep and the existing `services/trading_calendar.py` wrapper.

---

## Per-Library / Per-Target Analysis

### 1. nautilus_trader Databento LIVE adapter internals

**Versions:** ours=1.223.0, latest≈1.224.x (verify before any bump).

**How live data flows (data client → engine → strategy):**

- The adapter `DatabentoDataClient(LiveMarketDataClient)` opens **one Rust `nautilus_pyo3.DatabentoLiveClient` per dataset** (`_get_live_client`), keyed by dataset string (`EQUS.MINI`, `GLBX.MDP3`). Cite `adapters/databento/data.py:166,310-325`.
- Every record arrives via a single Rust→Python callback `_handle_msg(pycapsule)` → `capsule_to_data(...)` → `self._handle_data(data)`. Imbalance/Statistics/Status/SubscriptionAck go through `_handle_msg_pyo3`. Cite `data.py:1651-1659,1627-1646`. `_handle_data` is the inherited `DataClient` path that publishes onto the MessageBus → DataEngine → subscribed strategies' `on_bar`/`on_data`/`on_quote_tick`/`on_trade_tick`.
- The DataClient sets `is_connected = True` **once** on connect success and only `False` on explicit `disconnect()`. Cite `data/client.pyx:116,121-131` and the base wiring `live/data_client.py:529-539` (`_set_connected(True)` is the `connect()` success action). **It is NOT toggled on a feed drop** — the Rust client auto-reconnects underneath (`reconnect_timeout_mins`, default **10**, `config.py:53-57,71`). So `is_connected` is useless as a staleness signal.

**Per-subscription last-event timestamps / health hook — does the adapter expose them?**

- **NO.** There is no per-subscription `last_event_ts`, no per-feed heartbeat surfaced to Python, and no disconnect/reconnect callback exposed on the adapter. The only Python observation point is the `_handle_msg` / `_handle_msg_pyo3` callbacks. `subscribed_bars()` / `subscribed_quote_ticks()` exist (`data/client.pyx:337,392`) but return only the _set_ of subscriptions, not freshness.
- On disconnect the Rust client neither raises to Python nor flips `is_connected`; it retries internally (rapid retries → exponential backoff, up to `reconnect_timeout_mins`). A reconnect-storm is therefore **invisible** at the Python adapter layer except as a gap in `_handle_msg` calls.

**Is there a built-in Nautilus data-liveness monitor?** No first-class "feed is stale" component. `ComponentState` (DEGRADED/FAULTED) is a lifecycle state machine, not a data-freshness signal. The DataEngine builds internally-aggregated time bars but does not emit a "missed external bar" event. **Conclusion: freshness must be measured by observing event arrival time; Nautilus gives us the event stream and a `LiveClock`, nothing more.**

**Recommended pattern (per the project's "use the Nautilus API, don't reinvent" rule):**

1. Observe the live event stream the supported way — a **Nautilus `Actor`** (or the existing strategy mixin) subscribed to the required feeds records `clock.timestamp_ns()` per `(dataset, instrument_id, bar_type)` on each `on_bar`/`on_data`. An Actor is the native Nautilus primitive for "watch data without trading"; this is NOT reinventing the data client — it consumes the adapter's published events. (Actor `clock` is wired on register: `common/actor.pyx:726`.)
2. The freshness verdict = `now − last_event_ts` vs a session-aware expected-interval + grace. This mirrors the **existing `IBDisconnectHandler` pattern** (`services/nautilus/disconnect_handler.py`): an injected-dependency async loop inside the subprocess that, on grace-exceeded, sets the fleet halt latch and fires `on_halt`. Reuse that shape rather than inventing a new monitor primitive.
3. The `mbo_subscriptions_delay`, `timeout_initial_load`, and `reconnect_timeout_mins` adapter knobs (`config.py`) are relevant tunables but are NOT freshness signals.

**Sources:**

1. [adapters/databento/data.py (installed)](backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/data.py) — accessed 2026-06-03
2. [adapters/databento/config.py (installed)](backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/config.py) — accessed 2026-06-03
3. [live/data_client.py + data/client.pyx (installed)](backend/.venv/lib/python3.12/site-packages/nautilus_trader/data/client.pyx) — accessed 2026-06-03

**Design impact (CHANGES the design):** Do **not** plan to read a Nautilus connection-state flag or any adapter-exposed `last_event_ts` — neither exists, and `is_connected` will read `True` straight through a silent reconnect-storm. Freshness MUST be derived from observing the event arrival times of the required feeds (Actor/mixin recording `clock.timestamp_ns()` per subscription), evaluated against a session-aware grace. The "disconnect" failure mode is detected the SAME way as the other four — as an absence of events past grace — because the adapter hides the socket state. Mirror `IBDisconnectHandler`, don't build a parallel monitor framework.

**Test implication:** The harness cannot assert against a Nautilus connection flag. It must drive the freshness evaluator with a controllable clock + an injected `last_event_ts` source (the project's `IBDisconnectHandler` tests already prove this injected-loop pattern is unit-testable without a live runtime). All five failure modes reduce to "what `last_event_ts` does the evaluator see vs. wall-clock vs. session" — so the harness injects timestamps/event-gaps rather than real sockets. Add an explicit test that a silent reconnect (events resume before grace) does NOT halt, and that `is_connected==True` during a stall does NOT suppress the halt.

---

### 2. Nautilus reconciliation-completion signal (for the fail-closed `/resume` gate)

**The signal exists and is a clean boolean + event:**

- `LiveExecutionEngine.reconcile_execution_state(timeout_secs=10.0) -> bool` is the startup-reconciliation entry point; it returns **`True` on success** (all clients reconciled) / `False` on any failure, and in its `finally` block **always** sets `self._startup_reconciliation_event` (an `asyncio.Event`). Cite `live/execution_engine.py:1609-1746` (return `all(results)` at :1743; `_startup_reconciliation_event.set()` at :1746).
- The event is cleared at engine start (`:375`) and awaited by the continuous-reconciliation loop (`:688` "Awaiting startup reconciliation completion"). Public accessors: `reconciliation` property (`:260`) and `get_reconciliation_task()` (`:337`).

**Recommended pattern:** The node subprocess already `await`s reconciliation at startup (see `nautilus.md` gotcha #10). For `/resume`, the **node** is the only place that holds the engine; the supervisor/API does not. So the reconciliation-complete signal for `/resume` should be a **per-node flag the subprocess publishes** (e.g., to Redis) after `reconcile_execution_state()` returns `True` AND `_startup_reconciliation_event` is set — NOT something the API can read off the engine directly. This resolves PRD Open Question (line 279) toward "supervisor/node-reported flag," because the API has no engine handle and the reconciliation `asyncio.Event` lives inside the subprocess's event loop.

**Sources:**

1. [live/execution_engine.py (installed) lines 1609-1746](backend/.venv/lib/python3.12/site-packages/nautilus_trader/live/execution_engine.py) — accessed 2026-06-03
2. [.claude/rules/nautilus.md gotchas #10, #19](.claude/rules/nautilus.md) — accessed 2026-06-03

**Design impact (CHANGES the design):** `/resume`'s "reconciliation complete" precondition cannot be checked by the API process — there is no engine handle outside the node subprocess. Plan a node→Redis published reconciliation-complete marker (set after `reconcile_execution_state()` returns True). Treat `False`/timeout/absent as NOT-complete → `/resume` refuses (fail-closed). Note gotcha #10: a reconcile timeout still lets the node "look" alive, so the marker must reflect the **boolean return**, not merely that the node started.

**Test implication:** `/resume` tests must cover three reconciliation states independently of feed-freshness: complete-True (allow if feeds warm), incomplete/absent marker (refuse), and timeout-False (refuse). Pair with the feed-warm matrix so the four PRD `/resume` refusal cases (still-stale, warms-then-restales, reconciliation-incomplete, reconciliation-never-completes) each have a test.

---

### 3. databento Python SDK live client — heartbeats / liveness

**Important scoping fact:** MSAI's live path uses the **Rust `nautilus_pyo3.DatabentoLiveClient`** wrapped by the Nautilus adapter (target 1), **not** the pure-Python `databento.live` SDK client. So the SDK's heartbeat API is **not directly available** to MSAI's production code — but it is highly relevant to the **simulated-feed test harness** and documents the gateway behavior the Rust client relies on.

**Findings (installed SDK + docs):**

- The Databento live gateway sends periodic **heartbeat system messages even when no market data prints**. The SDK protocol exposes `heartbeat_interval_s` on the auth/connect path and detects them via `databento_dbn.SystemMsg.is_heartbeat()`. Cite installed `databento/live/protocol.py:62-63,79,89,397-399,453,463`.
- Client-side stale detection: per Databento docs, `NextRecord` raises a heartbeat-timeout if no record arrives within `heartbeat_interval + ~5s` (default total ≈ **35s**); the SDK supports auto-reconnect and a `Live.add_reconnect_callback`. (Web sources below.)

**Recommended pattern:** Because heartbeats arrive even on a silent symbol, "no trades printing" is NOT the same as "feed dead." A grace budget keyed purely on _trade/bar_ arrival would false-halt a legitimately quiet symbol during its session. The freshness signal should distinguish "feed alive but symbol quiet" (heartbeats/other-symbol events on the same dataset are arriving) from "feed dead" (NO events at all on the dataset). The Rust client's internal reconnect (`reconnect_timeout_mins=10`) means a true dead feed manifests as event silence up to ~10 min before the Rust side gives up — the freshness grace must be tighter than that for the trading use case.

**Sources:**

1. [databento/live/protocol.py (installed)](backend/.venv/lib/python3.12/site-packages/databento/live/protocol.py) — accessed 2026-06-03
2. [System messages | Databento Live API](https://databento.com/docs/api-reference-live/basics/system-messages) — accessed 2026-06-03
3. [Live.add_reconnect_callback | Databento Live API](https://databento.com/docs/api-reference-live/client/add-reconnect-callback) — accessed 2026-06-03

**Design impact (CHANGES the design):** Freshness must be evaluated at **two granularities** — per-dataset "feed alive" (any event, incl. other symbols on the dataset) and per-subscription "symbol fresh." A single-symbol stall (US-006 mode) is detected at the symbol granularity while the dataset is still alive; a disconnect/dead-feed is detected at the dataset granularity (all symbols silent). Do NOT halt purely on "this symbol printed no trade" during RTH for a thinly-traded name — the bar cadence (1-min OHLCV always produces a bar) is the right per-symbol cadence to measure, not trade prints.

**Test implication:** The simulated harness should model the gateway heartbeat: a "quiet symbol, live dataset" scenario must NOT halt (heartbeat/other-symbol events present), while "single-symbol stall past its bar cadence" MUST halt with that symbol's attribution, and "whole-dataset silence" MUST halt with dataset attribution. Use `SystemMsg.is_heartbeat()`-shaped synthetic records (or simply inject the two-granularity `last_event_ts` map) — no real Databento connection in CI (PRD AC US-006).

---

### 4. Session calendars for grace budgets

**The codebase ALREADY has this — no new dependency:**

- `exchange_calendars>=4.5,<5.0` is a direct dependency (`pyproject.toml:15`) and is mypy-overridden (`:114`).
- `services/trading_calendar.py` already wraps it: `XNYS` for equities/options/fx, **`CMES` for futures (CME Globex)**, lazy import, `@lru_cache`, `trading_days()` + `asset_class_to_exchange()`. Cite `trading_calendar.py:42-97`.
- `exchange_local_today()` (Chicago-anchored "today") lives at `services/nautilus/live_instrument_bootstrap.py:61` and is used widely (security_master, live API, supervisor). **Hard-won lesson** (`feedback_alias_windowing_must_use_exchange_local_today`): always use `exchange_local_today()` (Chicago), never `date.today()`/UTC, for any session-boundary computation — a late-Central-hours boundary bug already bit the alias resolver.
- `services/nautilus/market_hours.py` (`MarketHoursService`) computes RTH/ETH/cross-midnight windows from the instrument registry's `trading_hours` JSON (IB-sourced), with fail-open on missing metadata. This is the **per-instrument RTH/pre/post** source already wired into `RiskAwareStrategy._market_hours_check`.

**Gaps for PR 1b's grace logic:**

- `exchange_calendars` gives trading _days_ and session open/close, but `trading_calendar.py` currently exposes only `trading_days()` (day membership), not intraday session-phase (`pre`/`rth`/`post`/`closed`) for equities, nor the **CME GLBX daily maintenance window** (the ~1h break, typically 16:00–17:00 CT) that legitimately idles the futures feed. `XNYS` via exchange_calendars covers RTH open/close; pre/post for EQUS.MINI and the GLBX maintenance break need either `MarketHoursService` (registry `trading_hours`, IB-sourced — already has ETH + cross-midnight) or a small session-phase helper on top of the existing calendar.

**Recommended pattern:** Reuse `trading_calendar._calendar()` (exchange_calendars `CMES`/`XNYS`) for "is today a session day + session open/close," and reuse `MarketHoursService` for per-instrument RTH/ETH and the cross-midnight/maintenance windows it already models from IB `trading_hours`. Anchor every "now/today" in `exchange_local_today()` / Chicago. Build the grace-phase resolver as a thin composition over these two existing services — do NOT add `pandas_market_calendars` (not a dep; `exchange_calendars` is the project standard).

**Sources:**

1. [services/trading_calendar.py](backend/src/msai/services/trading_calendar.py) — accessed 2026-06-03
2. [services/nautilus/market_hours.py](backend/src/msai/services/nautilus/market_hours.py) — accessed 2026-06-03

**Design impact (CHANGES the design):** No new calendar dependency. Compose the grace-budget session resolver from `trading_calendar` (session day + open/close, CMES/XNYS) + `MarketHoursService` (RTH/ETH/cross-midnight from registry `trading_hours`). The GLBX daily maintenance window and equities pre/post are the two phases NOT directly exposed today — the design must add a session-phase helper, ideally extending `MarketHoursService`/`trading_calendar` rather than a standalone module. All boundaries use `exchange_local_today()` (Chicago), not UTC `date.today()`.

**Test implication:** Negative-case (no-false-halt) tests MUST cover: equities RTH/pre/post/closed boundaries, the natural 1-min closing-bar gap (PRD edge case), and the **CME GLBX maintenance-window idle** (feed legitimately silent → no halt). Use `freezegun` (already a dev dep) to pin Chicago-local times across each phase boundary; assert no halt during closed/maintenance and a halt when a feed is silent past grace _within_ an open session.

---

### 5. Existing MSAI halt-latch + metrics + supervisor plumbing (internal grounding)

**Halt latch (PR 1, #84) — `core/halt_keys.py`:**

- Fleet latch key: `msai:risk:halt` via `fleet_halt_key()`. Account latch: `msai:risk:halt:account:{account_id}` via `account_halt_key()` (keyed by `account_id`, NOT `ib_login_key`). Cite `core/halt_keys.py:17,28-48`.
- `HaltCause` StrEnum **already defines `DATA_STALE = "data_stale"`** alongside `FLEET_EMERGENCY` (manual /kill-all) and `OPERATOR_DRAIN` (account drain). Cause companion key via `halt_cause_key(scope, account_id=...)` → `…:cause` / `…:account:{id}:cause`. Cite `halt_keys.py:20-59`. **PR 1b's cause value is pre-reserved — reuse it, do not add a new enum member.**

**Inconsistency to resolve (P-flag for plan):** The reference implementation `IBDisconnectHandler._fire_halt()` writes **ad-hoc** keys `msai:risk:halt:reason` and `msai:risk:halt:source` (`disconnect_handler.py:70-71,222-228`), NOT the canonical `halt_cause_key()` companion. Meanwhile `/resume` clears `{_HALT_KEY}:set_by`, `{_HALT_KEY}:set_at`, AND `halt_cause_key("fleet")` (`api/live.py:3190-3196`) — it does NOT clear `:reason`/`:source`. So a data-stale halt written like `IBDisconnectHandler` would leave stale `:reason`/`:source` after resume. **PR 1b must standardize on `halt_cause_key()` for cause attribution and ensure `/resume` clears whatever cause keys PR 1b writes** (No Bugs Left Behind).

**Node-side order gate (PR 2/F6) — `risk_aware_strategy.py`:** `RiskAwareStrategy` overrides `submit_order`/`submit_order_list`/`modify_order`; the halt branch reads `_halt_cache` (`(halted, monotonic_ts)`, fed by a ≤1s background refresh task) and fails closed when `None`/stale(>`HALT_CACHE_MAX_AGE_S`=2.0s)/`True`; reduce-only/`MARKET_EXIT` always allowed; armed only in live via `_halt_gate_armed`. Cite `risk_aware_strategy.py:82-90,301-334,438-473,496-502`. **PR 1b sets the SAME fleet latch the gate already reads** — so once the data-stale halt sets `msai:risk:halt`, the existing gate blocks new opening orders on every node within ≤2s with NO change to the gate. This satisfies US-001 AC "node-side gate blocks new opening orders under the data-stale latch."

**API routes — `api/live.py`:** `/kill-all` (`:2591`), `/drain/{account_id}` (`:2881`), `/resume` (`:3166`), `/resume/{account_id}` (`:3227`), `/status` (`:3277`). `/resume` today is a **pure latch clear with no preconditions** — it deletes `_HALT_KEY` + companions and returns `resumed=True`. Cite `api/live.py:3166-3224`. PR 1b's fail-closed `/resume` (US-004) requires ADDING the feed-warm re-probe + reconciliation-complete gates BEFORE the delete. No `data-health` route exists yet — it's net-new.

**Metrics — hand-rolled registry (`services/observability/`):** NOT `prometheus_client`. `metrics.py` `Gauge`/`Counter`/`Histogram` accept arbitrary `**labels` and `.labels(...)` children (cite `metrics.py:141-176`). `trading_metrics.py` has `IB_DISCONNECTS` (unlabeled counter, `:68`) and labeled examples (`LIVE_INSTRUMENT_RESOLVED_TOTAL` uses `source`/`asset_class` labels applied at inc-time, `:74-82`). **Per-account/node/dataset/symbol labeling (US-005) is supported** by the existing primitive — add new labeled `Gauge`s (freshness age, warm/stale, connection-health) in `trading_metrics.py`. Cardinality must be bounded to _active subscriptions_ (PRD US-005 edge case), not the full universe.

**Sources:**

1. [core/halt_keys.py](backend/src/msai/core/halt_keys.py) — accessed 2026-06-03
2. [services/nautilus/disconnect_handler.py](backend/src/msai/services/nautilus/disconnect_handler.py) + [services/nautilus/risk/risk_aware_strategy.py](backend/src/msai/services/nautilus/risk/risk_aware_strategy.py) — accessed 2026-06-03
3. [api/live.py /resume + /kill-all](backend/src/msai/api/live.py) + [services/observability/trading_metrics.py](backend/src/msai/services/observability/trading_metrics.py) — accessed 2026-06-03

**Design impact (CHANGES the design):** (a) Reuse `HaltCause.DATA_STALE` + `halt_cause_key()` for attribution — already reserved; (b) standardize cause keys and FIX the `IBDisconnectHandler` `:reason`/`:source` vs `halt_cause_key()` divergence so `/resume` clears them (else stale cause after resume); (c) the node-side order gate needs ZERO changes — setting `msai:risk:halt` is sufficient to block new opening orders fleet-wide ≤2s; (d) `/resume` must grow real preconditions before the existing latch-delete; (e) `data-health` is a net-new GET route + CLI; (f) US-005 labels fit the existing hand-rolled metric primitive.

**Test implication:** Reuse the `IBDisconnectHandler` injected-loop test style for the freshness monitor. Add: a metrics-shape test asserting the freshness/health series carry `{account, node, dataset, symbol}` labels (US-005 success metric); a `/resume` precondition matrix (feed-warm × reconciliation-complete); a cause-attribution test that a data-stale halt and a manual `/kill-all` produce distinct, non-collapsing `HaltCause` values surviving a process restart (US-002 AC — cause persisted in Redis with TTL, not memory-only); and a test that a manual kill-all landing on top of an existing data-stale halt does not silently lose the original cause (US-002 edge case).

---

## Not Researched (with justification)

- **PyJWT / Azure Entra auth** — `data-health` + `/resume` reuse the existing `get_current_user` dependency; no auth change in scope (PRD §6 "same authz as existing live endpoints"). Covered by existing auth tests.
- **arq / Redis client** — the halt latch + cause keys use the same `redis.asyncio` client and patterns already proven in PR 1/2; no version-sensitive new usage.
- **ib_async / IB adapter** — IB is execution-only in this topology (PRD §5, Non-Goal: IB market-data codes 100/162/420 explicitly out of scope). The reconciliation signal (target 2) is the only IB-adjacent surface and is researched.
- **FastAPI / Pydantic / Typer** — standard route + schema + CLI patterns already established repo-wide; no new framework behavior.

## Open Risks

1. **Rust client reconnect masks the failure window.** The wrapped `DatabentoLiveClient` retries internally up to `reconnect_timeout_mins` (default 10) and never surfaces socket state to Python. A "disconnect" is only observable as event silence. The grace budget must be tighter than the Rust reconnect ceiling, and the design must NOT assume any Nautilus disconnect callback. (Target 1.)
2. **Two-granularity freshness is mandatory, not optional.** Heartbeats keep a dataset "alive" even when a symbol is quiet; a per-trade grace would false-halt thin names. Per-symbol freshness must use the guaranteed 1-min bar cadence, per-dataset must use any-event arrival. Getting this wrong fails either the false-halt metric (too tight) or the single-symbol-stall mode (too loose). (Target 3.)
3. **Cause-key divergence (active bug surface).** `IBDisconnectHandler` writes `:reason`/`:source`; `/resume` clears `halt_cause_key()` companions but NOT `:reason`/`:source`. PR 1b must converge on `halt_cause_key()` and make `/resume` clear everything it writes, or a data-stale cause persists past resume. (Target 5 — No Bugs Left Behind.)
4. **`/resume` reconciliation signal crosses a process boundary.** The reconciliation `asyncio.Event` + boolean live inside the node subprocess loop; the API cannot read them. Requires a node→Redis published reconciliation-complete marker reflecting the **boolean return** (not just "node started" — gotcha #10). If that marker is never published (node crash mid-reconcile), `/resume` must fail-closed. (Target 2.)
5. **Metric cardinality.** Per-symbol labels on freshness/health gauges must be bounded to active subscriptions; a high-cardinality universe would bloat the hand-rolled registry. (Target 5 / PRD US-005 edge case.)
6. **Version bump temptation.** Research is against installed 1.223.0 / databento 0.71.0. A nautilus bump within this PR would invalidate the cited `file:line` reconciliation/adapter internals (Cython/pyo3 internals shift between minors) — do NOT bump in PR 1b.
7. **`exchange_calendars` gives days + open/close but not equities pre/post nor the GLBX maintenance break directly.** The session-phase helper must compose `trading_calendar` + `MarketHoursService`; an incomplete phase model risks false-halts during pre/post/maintenance idle. (Target 4.)
