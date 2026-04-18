# Changelog

All notable changes to msai-v2 will be documented in this file.

## [Unreleased]

### Added

- Initial project setup with Claude Code configuration
- 2026-04-16: Portfolio-per-account-live PR #1 — live-composition schema + domain layer (branch `feat/portfolio-per-account-live`). Pure additive, zero live-risk. Four new tables (`live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `live_deployment_strategies`), two new columns (`live_deployments.ib_login_key`, `live_node_processes.gateway_session_key`), partial unique index (`uq_one_draft_per_portfolio`). Services: `compute_composition_hash`, `PortfolioService` (create + add_strategy + list_draft_members + get_current_draft with graduated-strategy invariant), `RevisionService` (snapshot with `SELECT … FOR UPDATE` serialization + identical-hash collapse + `get_active_revision` + `enforce_immutability` guard). 13 new integration tests (portfolio_service + revision_service + full_lifecycle + alembic round-trip). 5-advisor council approved; plan-review loop 3 iterations to clean. Nothing in `/api/v1/live/*`, supervisor, or read-path touched. Design doc: `docs/plans/2026-04-16-portfolio-per-account-live-design.md`. Implementation plan: `docs/plans/2026-04-16-portfolio-per-account-live-pr1-plan.md`.
- 2026-04-16: ES futures canonicalization pipeline (PR #23) — `canonical_instrument_id()`, `phase_1_paper_symbols()` as a function with fresh front-month per call, `exchange_local_today()` helper on America/Chicago, `TradingNodePayload.spawn_today_iso` threading so supervisor + subprocess agree on the same quarterly contract across midnight-on-roll-day spawns. 28 new unit tests in `test_live_instrument_bootstrap.py` (39 total). Branch `fix/es-contract-spec`.

- 2026-04-16: Portfolio-per-account-live PR #2 — semantic cutover (PR #29, branch `feat/portfolio-per-account-live-pr2`). New `POST /api/v1/live/start-portfolio` endpoint accepting `portfolio_revision_id + account_id`. Multi-strategy `TradingNode` via `TradingNodeConfig.strategies=[N ImportableStrategyConfigs]`. `FailureIsolatedStrategy` base class wraps event handlers via `__init_subclass__` to prevent one strategy crashing the node. Portfolio CRUD API (`/api/v1/live-portfolios`). `PortfolioDeploymentIdentity` replaces strategy-level identity. `LiveDeploymentStrategy` bridge rows for per-member attribution. Supervisor payload factory resolves portfolio members. Audit hook per-strategy tagging via `_resolve_strategy_id()` lookup. Cache-key namespace helper. 3 Alembic migrations (add FK, backfill, drop legacy columns). `strategy_id_full` format changed to `{class}-{order_index}-{slug}` for same-class disambiguation. 20 commits, 1341 unit tests, E2E 15/15 against live dev Postgres. 2-iteration code review loop (Codex + 5 PR-review-toolkit agents).
- 2026-04-17: Portfolio-per-account-live PR #3 — multi-login Gateway topology (PR #30, branch `feat/portfolio-per-account-live-pr3`). `GatewayRouter` resolves `ib_login_key → (host, port)` from `GATEWAY_CONFIG` env var. Per-gateway-session spawn guard (concurrent-startup check scoped to `gateway_session_key`). `gateway_session_key` populated on `LiveNodeProcess` at creation. Enforce NOT NULL on `ib_login_key` + `gateway_session_key` via migration. Resource limits on all live-critical Docker Compose containers. Recreated backfill migration lost in PR#2 squash merge.

### 2026-04-17 — db-backed-strategy-registry PR (scope-backed to backtest-only)

**Shipped:**

- New Postgres tables `instrument_definitions` + `instrument_aliases` (UUID PK, effective-date alias windowing for futures rolls).
- `SecurityMaster.resolve_for_backtest(symbols, *, start, end, dataset)` — registry lookup with Databento `.Z.N` continuous-futures synthesis on cold miss.
- `SecurityMaster.resolve_for_live(symbols)` — registry lookup with closed-universe `canonical_instrument_id()` fallback.
- `DatabentoClient.fetch_definition_instruments(...)` — download + decode `.definition.dbn.zst` with `use_exchange_as_venue=True` on `from_dbn_file()` call site.
- `msai instruments refresh --symbols ... --provider [interactive_brokers|databento]` CLI — pre-warm the registry before deploying strategies. (Databento path works; IB path deferred — see Deferred section.)
- `SecurityMaster.__init__` relaxed: `qualifier` and `databento_client` both optional (same class now serves backtest + live callers).
- Continuous-futures helpers: `is_databento_continuous_pattern`, `raw_symbol_from_request`, `ResolvedInstrumentDefinition`, `resolved_databento_definition`, `definition_window_bounds_from_details`, `continuous_needs_refresh_for_window`, `raw_continuous_suffix` (reserved).
- Backtest API wired: `POST /api/v1/backtests/run` now resolves via registry (`api/backtests.py:90`).
- Split-brain normalization: `.XCME` → `.CME` across source docstrings + 26 test fixtures.
- `.. deprecated::` notices added to `instruments.py` + `live_instrument_bootstrap.py` (modules remain load-bearing for closed-universe live path + live-supervisor payload factory).

**Tests:** 1366 unit passes + ~40 integration tests (including full-lifecycle, backtest/live parity via freezegun, Cache-Redis roundtrip, continuous-futures placeholder). Zero regressions from the registry work.

**Architectural decisions (after 5 plan-review iterations):**

- `InstrumentDefinition.instrument_uid` is UUID, never venue-qualified string. Venue-qualified aliases live in `instrument_aliases` rows with effective-date windowing — futures rolls are row updates, not PK migrations.
- Runtime canonical = exchange-name (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`). Matches IB adapter defaults.
- `asset_class` DB enum = `equity|futures|fx|option|crypto` (note plural `futures` — matches CHECK constraint, diverges from codex's `stocks`/`options`).
- Nautilus's `Cache(database=redis)` owns `Instrument` payload durability — MSAI registry holds only control-plane metadata. Verified end-to-end in `test_cache_redis_instrument_roundtrip.py`.
- Schema bug caught during Task 9 testing: `instrument_aliases.venue_format` widened `String(16)` → `String(32)` in-place (pre-merge).

**Deferred to follow-up PRs (not in this PR):**

- **Live-path wiring.** Plan attempted 3 architectures (A: supervisor calls SecurityMaster inline — blocked by no IBQualifier; B: persist canonicals on revision_strategies — blocked by composition_hash immutability; C: payload-dict hint — blocked by supervisor deliberate ignore). Option D candidate (persist on `LiveDeployment`, warm-cache-only at API) pending its own design pass. Skeleton: end of `docs/plans/2026-04-17-db-backed-strategy-registry.md`.
- **InstrumentCache → Registry migration.** Existing `instrument_cache` table (Nautilus payloads + trading_hours + IB contract JSON) coexists with new registry. Needs its own PR to migrate 7 call sites + trading_hours relocation.
- **Pydantic config-schema extraction on `StrategyRegistry`.** Orthogonal to registry; deferred.
- **IB provider factory in `msai instruments refresh`.** Needs `Settings` expansion (ib_request_timeout_seconds, etc.) — ships with the live-wiring follow-up.

**Known limitations discovered post-Task 20 (Codex Phase 5 review):**

- **`msai instruments refresh --symbols <plain>` works only for `.Z.N` continuous-futures.** For plain symbols (`AAPL`, `ES`), the CLI delegates to `SecurityMaster.resolve_for_backtest`, which raises `DatabentoDefinitionMissing` because no fetch-and-synthesize path exists for non-continuous symbols. **Workaround:** operators seed plain-symbol registry rows via direct SQL until the follow-up PR adds a proper Databento plain-symbol fetch. Example: `INSERT INTO instrument_definitions (raw_symbol, listing_venue, routing_venue, asset_class, provider, lifecycle_state) VALUES ('AAPL', 'NASDAQ', 'NASDAQ', 'equity', 'databento', 'active')` + matching alias row.

- **`resolve_for_backtest` uses today's date for alias windowing**, not the backtest's `start_date`. After a futures front-month roll, a historical backtest (e.g. `start_date=2025-12-01, end_date=2026-01-31`) will receive the **current** front-month alias rather than the contract active during the backtest window. **Workaround:** operators passing continuous-futures `.Z.N` patterns avoid this issue. For concrete futures with historical windows, operators must manually specify the correct contract (e.g. `ESZ5.CME` for Dec-2025 backtests). Follow-up: thread `start_date` into `InstrumentRegistry.find_by_alias` within `resolve_for_backtest`.

- **Worker parquet lookup assumes raw-symbol == canonical prefix.** `workers/backtest_job.ensure_catalog_data` passes `Backtest.instruments` (canonical IDs like `ESM6.CME`) to `catalog_builder.build_catalog_for_symbol`, which then calls `resolve_instrument()` and splits on `.` to derive the raw_symbol. For equities this happens to work (`AAPL.NASDAQ` → raw `AAPL`, parquet root is `AAPL/`), but for futures it fails (`ESM6.CME` → raw `ESM6`, parquet root is `ES/`). Fix 9 adds an optional `raw_symbol_override` kwarg to `build_catalog_for_symbol`/`ensure_catalog_data` so the worker can pass the user's original input; **wiring the worker + `Backtest.input_symbols` column is a follow-up** (see plan doc).

**Commits (22 total):** 21b9ec1, 3b2cc35, 7ea6fb1, 75a3cf1, 9282824, 15b2d22, 2fb64b1, 38edeb9, 2829585, 3c26ad3, a2b9b01, 32f0e57, c87751f, c17aef6, b39d318, 71c904b, bfe90e8, c84e697, 7383319, dce4f82, 7324e0b, plus this commit.

### Changed

- 2026-04-16: Live-supervisor now canonicalizes user-facing instrument ids before passing to strategy config — e.g., `ES.CME` → `ESM6.CME` for futures, identity for stocks/ETF/FX. Overwrites stale explicit `instrument_id` / `bar_type` only when the root symbol changes (futures rollover), preserving operator aggregation choices on stocks/FX.

### Fixed

- 2026-04-16: `/live/positions` empty while open position exists (PR #27, branch `fix/live-positions-empty`). Five compounding bugs: (1) `derive_message_bus_stream` returned `-stream` but Nautilus writes to `:stream` — every PositionOpened/OrderFilled/AccountState event silently dropped since the projection consumer was wired (Alembic `n2h3i4j5k6l7` normalizes existing rows); (2) Nautilus `Cache.cache_all()`/`positions_open()` silently drops rows — switched cold-path readers to `adapter.load_positions()`/`adapter.load_account()` directly; (3) `deployment.status` stuck at `starting` on warm-restart when process already `running` (UP-direction mirror of PR #26); (4) `/live/positions` filter now keyed on latest-process-row status via subquery instead of stale `deployment.status`; (5) `_to_snapshot`/`_to_account_update` couldn't parse Nautilus `Money` strings (`"0.00 USD"`), added `_money_to_decimal` helper. Live-verified on running stack (paper EUR/USD smoke): BUY 1 @ 1.17805 filled, `/live/positions` returned `[{qty:"1.00", avg_price:"1.17805", realized_pnl:"-2"}]` — first time a position was actually visible through this endpoint in the project's history.
- 2026-04-16: Deployment-status sync on normal stop + typed `HEARTBEAT_TIMEOUT` error (branch `fix/status-sync-and-typed-errors`). **Fix A:** `trading_node_subprocess._mark_terminal` now syncs the parent `LiveDeployment.status` on clean exit — previously only the spawn-failure path did this (X3 iter 1), so `/live/stop` left `live_deployments.status='running'` indefinitely. **Fix B:** new `FailureKind.HEARTBEAT_TIMEOUT` replaces opaque `UNKNOWN` for stale-heartbeat sweeps; `HeartbeatMonitor` also syncs parent deployment.status. Endpoint classifier + idempotency cache accept `HEARTBEAT_TIMEOUT` in `permanent_kinds`, so retries with same Idempotency-Key return structured `503 {failure_kind: "heartbeat_timeout"}` instead of "unknown failure". Live-verified on running stack: injected stale process row on orphan `starting` deployment, supervisor sweep flipped both rows within one 10s cycle. 1209 unit tests pass (+1 parametrize).
- 2026-04-16 14:52 UTC: **First live real-money drill success on `U4705114` (MarketSignal.ai LLC, mslvp000/test-lvp)**. Deployment `5828fe02` deployed `SmokeMarketOrderStrategy` against `AAPL.NASDAQ` with `paper_trading=false`; bars flowed in real-time via API, smoke fired `BUY 1 AAPL MARKET` at 14:52:30, filled at $261.33 (commission $1.00, broker_trade_id `0002264f.69e1362b.01.01`). Validated live: PR #23 canonicalization ✓, PR #21 `side="BUY"` string ✓, PR #24 WS reconnect returns 3 hydrated trades ✓. Nautilus ExecEngine startup reconciliation also surfaced pre-existing external positions on the account (SPY 156 @ $658.04, EEM 309 @ $49.06) as `inferred OrderFilled` audit rows — noted as follow-up to distinguish reconciliation-inferred fills from strategy-submitted fills in `audit_hook.py`. Env setup required: `IB_PORT=4003`, `TRADING_MODE=live`, `IB_ACCOUNT_ID=U4705114`, `TWS_USERID=mslvp000`. Also required adding `IB_PORT` + `TRADING_MODE` var declarations to the live-supervisor env block in `docker-compose.dev.yml` (they were previously absent, so env overrides weren't propagating into the container).
- 2026-04-16: WebSocket reconnect snapshot now hydrates `orders` / `trades` / `status` / `risk_halt` alongside `positions` / `account` (PR #24, branch `feat/live-state-controller`). Phase 2 #4 narrow Option B — engineering council rejected the 1,200 LOC LiveStateController port as too risky pre-drill, approved this 150 LOC augmentation that reuses claude's existing authoritative read models (`OrderAttemptAudit`, `Trade`, `ProjectionState`). Structured log `ws_snapshot_emitted` emits all counts per connect. Also fixed a pre-existing cold-path crash in `position_reader._read_via_ephemeral_cache_account` (bare `AccountId("DUP733213")` → `ValueError: did not contain a hyphen`) that was silently closing every fresh-backend WS snapshot with 1011 — now qualifies with `INTERACTIVE_BROKERS-` prefix to match Nautilus's `AccountState` format. 14 new unit tests (1208 total). E2E verified against paper IB Gateway: all 8 snapshot keys arrive on the wire, 50 real EUR/USD trades round-trip through the reader, structured log emits as specified.
- 2026-04-16: ES deployments producing zero bar events (drill 2026-04-15 failure mode, PR #23) — root cause was an instrument-id mismatch between the user-facing `ES.CME` bar subscription and the concrete `ESM6.CME` instrument Nautilus registers after IB resolves `FUT ES 202606`. Now canonicalized at the supervisor. Live-verified: subscription succeeds against paper IB Gateway with no "instrument not found" error. Also caught a `.XCME` (ISO MIC) vs `.CME` (IB_SIMPLIFIED native) venue bug in an earlier iteration that would have shipped without the live e2e test. NOTE: bars still don't fire due to a broader IB entitlement gap on the account tied to `DUP733213` — NOT a code bug. Confirmed via direct `ib_async` probes against the paper gateway (port 4004): IB error **354** for CME futures (ES) and IB error **10089** for NASDAQ-primary equities (AAPL, `"Requested market data requires additional subscription for API. AAPL NASDAQ.NMS/TOP/ALL"`). Open question: user reports trading SPY/QQQ on IBKR "for years" which contradicts the 10089 error — possible explanations: (a) trading was via TWS desktop, which honors different subscription gating than the API (many entitlements need a separate "enable for API" checkbox); (b) trading was on a different user login (`pablo-data`, `apis1980`, etc.) with its own subscription list; (c) paper accounts don't auto-inherit live subscriptions without an explicit "Share Live Market Data With Paper Account" toggle. EUR/USD (IDEALPRO FX) is the only asset class currently producing real-time bars through the Nautilus live path.

### Removed

---

## Format

Each entry should include:

- Date (YYYY-MM-DD)
- Brief description
- Related issue/PR if applicable
