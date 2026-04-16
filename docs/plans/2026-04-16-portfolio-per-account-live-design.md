# Portfolio-Per-Account Live Trading — Design

**Date:** 2026-04-16
**Status:** Approved (user + 5-advisor council + Nautilus audit)
**Branch:** `feat/portfolio-per-account-live`

---

## Goal

Replace MSAI's strategy-per-account `LiveDeployment` model with a **portfolio-per-account** primitive so that:

1. A portfolio = a collection of graduated strategies (one strategy = one asset trader) with weights and rebalance history.
2. The same portfolio can be deployed to N IB accounts in parallel.
3. Two portfolios cannot share an account (user constraint — out of scope).
4. Strategy-to-portfolio is M:N (one strategy can live in multiple portfolios, like an analyst on multiple teams).
5. Portfolio weights change over time as the portfolio manager rebalances; each rebalance is a new immutable revision, which is the warm-restart identity boundary.

## Lifecycle

```
Strategy (research artifact, one-asset trader)
    └─> GraduationCandidate (passed backtest + walk-forward)
         └─> added to ≥1 LivePortfolio (M:N)
              └─> rebalance → LivePortfolioRevision (immutable snapshot, hashable)
                   └─> deploy to account → LiveDeployment = (revision_id, account_id)
                        └─> runtime → one Nautilus subprocess per IB login
                             └─> multiple exec_clients (one per account on that login)
                                  └─> strategies list (all portfolio members)
```

## Key decisions

### 1. New schema (not reuse of research Portfolio)

The existing research `Portfolio` + `PortfolioAllocation` tables are tied to `GraduationCandidate` with allocation = capital weights across candidates. That is a **research-phase** object, not a **live-deployment-phase** object. The council's Maintainer + Contrarian agreed: reusing it confuses semantics and couples live restart identity to a mutable research row.

New tables:

- `live_portfolios` — mutable identity (name, objective, description, created_by, `latest_revision_id` pointer)
- `live_portfolio_revisions` — immutable snapshot; one revision per rebalance; `composition_hash` (sha256 of sorted member tuples)
- `live_portfolio_revision_strategies` — M:N membership: `(revision_id, strategy_id, config_hash, instruments_signature, weight, order_index)`
- `live_deployment_strategies` — per-deployment member rows so read path (WebSocket snapshot, `/live/positions`, audit) can attribute events to the right strategy via `strategy_id_full`

New columns:

- `live_deployments.portfolio_revision_id` (FK) replaces `strategy_id`
- `live_deployments.ib_login_key` (TWS username, used by supervisor to multiplex)
- `live_node_processes.gateway_session_key` (maps to `(ib_host, ib_port, ib_login)` tuple for spawn-guard scoping)

### 2. Runtime topology — **Nautilus-native multi-account**

PR #3194 (Nautilus 1.225, merged 2026-01-12) added native multi-account live trading: one `TradingNode`, multiple `exec_clients` keyed by account alias, each with its own `ibg_client_id` + `account_id`, shared data client. Per-account queries via `portfolio.realized_pnl(account_id=...)`, `cache.account(AccountId(...))`.

**Implication:** the subprocess topology is **one subprocess per `ib_login_key`**, not per account. Multiple `LiveDeployment` rows that share an IB login collapse into one subprocess with a multi-exec-client `TradingNodeConfig`. Different IB logins remain separate subprocesses (different Gateways).

- Accounts under the same IB advisor login (e.g., `DUP733211` / `DUP733212` under `marin1016test`): 1 subprocess, 1 Gateway, N exec_clients.
- Accounts under different logins (e.g., `mslvp000` vs `marin1016test`): N subprocesses, N Gateways.

`LiveDeployment` identity is still `(portfolio_revision_id, account_id)` — that's the **logical** deployment. The supervisor multiplexes logical deployments onto the fewest subprocesses that respect IB's single-session-per-login constraint.

### 3. IB Gateway Compose topology

Static Compose services per IB login:

```
ib-gateway-marin1016test (port 4004)
ib-gateway-mslvp000       (port 4005)
ib-gateway-pablo-data     (port 4006)
...
```

`account_id → ib_login_key → (host, port)` resolved at deploy time. Adding a new login requires a Compose edit + restart — acceptable for single-operator, small-N.

### 4. Warm restart identity

`identity_signature = sha256(canonical_json({
    portfolio_revision_id,
    account_id,
    paper_trading
}))`.

Any change to the revision's composition (strategies, weights, configs, instruments) produces a new `LivePortfolioRevision` with a new `composition_hash`, hence a new `identity_signature` → **cold restart**. Within a revision, the deployment is warm-restartable across process restarts.

**No per-strategy hot-swap in v1.** Council consensus; risk of partial state reload + cache-key collisions too high.

### 5. MSAI-specific runtime overlays (Nautilus won't do this)

- **Per-strategy failure isolation.** Nautilus single-threaded kernel does not isolate strategy exceptions (Discussion #2804, architecture doc "fail-fast"). MSAI provides a `FailureIsolatedStrategy` base class / mixin that wraps event handlers (`on_bar`, `on_quote_tick`, `on_order_event`) with `try/except`, logs the error with `strategy_id`, emits a per-strategy halt signal, and keeps the node running. ~80 LOC + tests.
- **Per-strategy cache namespace.** `cache.add(key, value)` has no strategy scope. MSAI's strategy base class prefixes all cache keys with `strategy_id:`.
- **Per-gateway-session spawn guard.** Replace the current global `CONCURRENT_STARTUP` sentinel with a filter keyed by `gateway_session_key`. Different logins → concurrent spawn allowed. Same login → serialized.
- **Deterministic `ibg_client_id` allocation.** Nautilus gotcha #3: two TradingNodes with the same client_id silently disconnect. Allocation rule: `client_id = base + exec_client_index` where base is derived from `hash(deployment_slug) % 900 * 10` (range 0–8999) and index is 0, 1, 2, … per exec_client inside the subprocess.

### 6. Known Nautilus footguns to guard

- **Issue #3176** (closed 2025-11): persistent Redis + `external_order_claims` creates duplicate orders on restart. Regression test required in the warm-restart path.
- **Issue #3655**: reconciliation doesn't pick up manual TWS cancellations mid-session. Document in operator runbook; no code change.
- **`load_state` / `save_state` default False.** Verify both are `True` in the subprocess's `TradingNodeConfig`; add a unit test that asserts both flags.

## Non-goals (v1)

- Per-strategy hot-swap within a running portfolio (cold restart for any member change).
- Two portfolios sharing one account.
- Dynamic IB Gateway spin-up via Docker API.
- Partial warm restart after some strategy files changed (composite revision hash means any change = full cold restart).
- Multi-tenant isolation (single operator).
- Frontend portfolio composition UI (backend CRUD API only for v1; UI is a separate PR track).

## PR sequencing (Option C revised)

### PR #1 — Schema + domain models (pure additive, zero live-risk)

- Alembic migration: `live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `live_deployment_strategies` tables; `ib_login_key` on `live_deployments`; `gateway_session_key` on `live_node_processes`.
- SQLAlchemy models + relationships.
- `PortfolioService` (create, add member, list members, active revision).
- `RevisionService` (snapshot + composition_hash + freeze).
- Unit tests: hash determinism, M:N mapping, revision immutability (cannot mutate after first deployment references it).

~600 LOC. Nothing in the live path uses the new tables yet. Ship + merge.

### PR #2 — Semantic cutover (live-critical)

- Portfolio CRUD API: `POST /portfolios`, `POST /portfolios/{id}/strategies`, `POST /portfolios/{id}/rebalance` (creates new revision).
- `/api/v1/live/start` accepts `portfolio_revision_id + account_id` instead of `strategy_id + instruments`.
- Supervisor payload + subprocess rewired: builds `TradingNodeConfig.strategies=[ImportableStrategyConfig, ...]` per member; multi-account `exec_clients` dict for deployments sharing an IB login.
- Read path (WebSocket snapshot, `/live/positions`, `/live/trades`, audit hook) queries `LiveDeploymentStrategy` rows — one `strategy_id_full` per member.
- **`FailureIsolatedStrategy` base class** + wrapper on event handlers.
- **Strategy-cache namespace** (`strategy_id:` prefix on `cache.add`).
- **`load_state/save_state=True` verification** in `build_live_trading_node_config`.
- **Regression test for issue #3176** restart duplicate-order path.
- Backfill migration: wrap each existing `LiveDeployment` row as a single-strategy portfolio with one allocation at weight=1.0.
- Drop `LiveDeployment.strategy_id` + `config_hash` + `instruments` + `strategy_code_hash` columns (data preserved on revision rows).

~1200 LOC. Single stop/start maintenance window; operator runs one deploy cycle to validate. Ship + merge.

### PR #3 — Multi-login Gateway topology

- Per-IB-login Compose Gateway services (`ib-gateway-{login}` pattern).
- `GatewayRouter` resolves `ib_login_key → (host, port)` from static config.
- Per-gateway-session spawn guard (filter `LiveNodeProcess` by `gateway_session_key`).
- Deterministic `ibg_client_id` allocation rule.
- Resource limits (`mem_limit`, `cpus`) on every live-critical container (Scalability Hawk's blocking objection #3).

~500 LOC. Enables same portfolio across accounts on different IB logins.

### Phase 2 (not covered here)

- Frontend portfolio composition UI.
- Dynamic Gateway spin-up when static Compose hits the 5+ account ceiling.
- VM split (trading VM + compute VM) when resource ceiling is reached.
- Per-strategy risk overlays (daily loss limit, max position notional) via decorator/mixin.

## Testing strategy

- **Unit:** hash determinism, revision immutability guard, M:N mapping, `FailureIsolatedStrategy` wraps every event handler, cache key namespacing, `load_state/save_state` flag assertion.
- **Integration (Postgres testcontainers):** portfolio CRUD, revision creation, deployment-to-revision FK, backfill migration, warm vs cold restart identity.
- **E2E use cases (Phase 3.2b):**
  - UC1 (API+runtime): Create portfolio → add 2 strategies → rebalance → deploy to paper account → both strategies reach RUNNING → /live/positions surfaces both strategies' instruments → /live/stop → deployment.status=stopped.
  - UC2 (API+runtime): Same portfolio revision deployed to 2 accounts under same IB login → one subprocess, two exec_clients, per-account position attribution correct.
  - UC3 (regression for #3176): Deploy → fill an order → restart backend → verify no duplicate order created on the new subprocess.
  - UC4 (failure isolation): Inject a strategy that raises in `on_bar` → other strategies in the portfolio keep running → halt signal emitted for the bad strategy.

## References

- Council verdict (this session): 5 advisors + Codex chairman, MINORITY REPORT preserved (Simplifier overruled on reusing research Portfolio; Scalability Hawk's per-strategy sub-hash deferred).
- Nautilus GitHub PR #3194 — multi-account live trading (https://github.com/nautechsystems/nautilus_trader/pull/3194)
- Nautilus issue #3176 — restart duplicate orders (https://github.com/nautechsystems/nautilus_trader/issues/3176)
- Nautilus Discussion #2804 — no per-strategy failure isolation (https://github.com/nautechsystems/nautilus_trader/discussions/2804)
- Nautilus Portfolio concept — position aggregation only, no composition (https://nautilustrader.io/docs/nightly/concepts/portfolio/)
- Architecture "fail-fast" doc (https://nautilustrader.io/docs/latest/concepts/architecture/)
