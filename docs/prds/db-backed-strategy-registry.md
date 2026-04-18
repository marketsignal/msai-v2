# PRD: DB-Backed Strategy Registry + Continuous Futures + Instrument Control-Plane

**Version:** 1.0
**Status:** Draft
**Author:** Claude (Opus 4.7) + Pablo
**Created:** 2026-04-17
**Last Updated:** 2026-04-17

---

## 1. Overview

MSAI v2's claude-version currently resolves trading instruments ad-hoc per call site: bare tickers like `AAPL` get a `NASDAQ` suffix via a `TestInstrumentProvider` wrapper; ES futures roll to `ESM6.CME` at spawn; Databento continuous futures (e.g. `ES.Z.5`) are unsupported. There is no persisted registry linking a strategy-authored symbol to the right Nautilus `InstrumentId`, no provider provenance, no lifecycle state, and no config-schema metadata for UI form generation.

This PR introduces a thin Postgres **instrument control-plane** (`InstrumentDefinition` + `instrument_alias` tables), keyed on a stable logical UUID (not a venue-string primary key), with exchange-name aliases as the runtime canonical (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`). It ports the codex-version's Databento `.Z.N` continuous-futures synthesis (a real Nautilus gap), wires `CacheConfig(database=redis)` so Nautilus owns `Instrument`-payload durability natively, and adds a Pydantic `model_json_schema()` extraction on `StrategyRegistry` for future UI form work. It also normalizes an existing XCME/CME split-brain in claude-version's docstrings and test fixtures.

**What makes this different from codex-version's 605-LOC `NautilusInstrumentService`:** MSAI's table is a _control-plane_, not a cache of serialized Nautilus payloads. Nautilus's own cache DB + Parquet catalog already handle durability (verified in venv). MSAI owns raw-symbol aliases, listing/routing venue split, continuous-roll policy, lifecycle state, and the Databento `.Z.N` synthesis Nautilus leaves open.

## 2. Goals & Success Metrics

### Goals

- **G1. Unified instrument resolution:** strategies declare plain symbols (`"AAPL"`, `"ES"`); backtest and live paths both resolve to the SAME `InstrumentId` via the same control-plane table.
- **G2. Survive future additions without schema rewrite:** primary key is logical (UUID); venue-qualified strings are aliases. Adding Polygon, Refinitiv, or a new broker = add alias rows, not migrate PKs.
- **G3. Support continuous futures in backtests:** Databento `.Z.N` pattern resolves to a synthetic continuous `Instrument` matching Nautilus's `FuturesContract` contract.
- **G4. Fix the current split-brain:** one runtime format across equities + futures + FX (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`); no more `.XCME` surviving in docstrings, test fixtures, or config docs.
- **G5. Delegate Instrument durability to Nautilus:** wire `CacheConfig(database=redis)` so MSAI's table never duplicates Nautilus-serialized payloads.
- **G6. Expose strategy config schemas:** `StrategyRegistry` returns `model_json_schema()` + defaults per strategy for a future UI form generator.

### Success Metrics

| Metric                                                                          | Target                                                                                            | How Measured                                                                                             |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Instrument resolution is warm-cache sync inside the strategy hot path           | 100% (no async/DB call inside `Strategy.on_bar`)                                                  | Grep `on_bar` / `on_quote_tick` / `on_trade_tick` call sites; integration test                           |
| Same strategy code runs backtest + live with identical `InstrumentId` strings   | 100% on current universe (AAPL, MSFT, SPY, ES, EURUSD)                                            | Parity test: backtest vs live-paper deployment of same strategy, assert ID equality                      |
| Continuous-futures backtest (`ES.Z.5`) produces bars                            | ≥ 1 passing integration test with real Databento data                                             | CI integration suite with a Databento definition fixture                                                 |
| Nautilus cache DB holds the `Instrument` payload; MSAI's table holds NO payload | Zero bytes of `Instrument`-serialized JSONB in Postgres                                           | Schema inspection — no `instrument_data` JSONB column on `InstrumentDefinition`                          |
| Split-brain normalized                                                          | 0 occurrences of `.XCME` in claude-version source/tests (except explicit legacy-input acceptance) | `grep -rn "\.XCME" claude-version/backend/` excluding `live_instrument_bootstrap.py:147` (legacy accept) |
| PR gate                                                                         | Unit + integration tests green; `ruff`, `mypy --strict` clean on new files                        | verify-app agent                                                                                         |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Polygon integration** — not in the MSAI stack (user-clarified). Databento is backtest-only + optional future real-time; IB is live real-time + execution.
- ❌ **Wholesale MIC-code migration** (`AAPL.XNAS`, `ES.XCME`) — deferred. Minority report preserved; can be adopted later by adding MIC-format alias rows, no schema rewrite.
- ❌ **UI for instrument registry browsing / config form rendering** — schema extraction ships (backend); UI consumption is a separate future PR.
- ❌ **Automatic futures roll scheduling** — `live_instrument_bootstrap.canonical_instrument_id(today=...)` already handles ES/NQ quarterly roll at spawn. No cron-driven roll in this PR.
- ❌ **Bulk backfill on migration** — empty table at ship; populate on next `/live/start` or ingest. Optional `msai instruments refresh` CLI for explicit pre-warming.
- ❌ **Options trading support** — schema includes `listing_venue` + `routing_venue` columns so the future options PR doesn't reshape the table, but no options-specific code paths ship here.
- ❌ **Cross-adapter canonicalization service** outside the registry (e.g. standalone `canonical_instrument_id()` helpers) — consolidate into `SecurityMaster.resolve_for_*` methods; deprecate the two existing `canonical_instrument_id` functions as per the maintainer's migration note.

## 3. User Personas

### Pablo (primary and sole user)

- **Role:** Owner-operator of MSAI. Designs strategies, runs backtests, deploys to IB paper + live accounts, monitors fills, debugs at 3am during drills.
- **Permissions:** All — single-user platform. Azure Entra ID JWT required for all authenticated endpoints.
- **Goals:** Trade multiple instruments across portfolios of strategies. Verify each fill against IB TWS. Ship new strategies weekly. Read logs without mental translation.
- **Knowledge assumed:** deep IB/TWS familiarity, Nautilus fluent, read-only across Polygon/Databento/Refinitiv vocabulary.

## 4. User Stories

### US-001: Strategy author declares plain symbols, registry resolves per context

**As a** strategy author
**I want** to declare `instruments=["AAPL", "ES", "EURUSD"]` in my strategy config
**So that** backtest and live deployments both receive the correct Nautilus `Instrument` objects without me hardcoding vendor-specific IDs

**Scenario:**

```gherkin
Given a strategy `MyStrategy` with config `{instruments: ["AAPL", "ES", "EURUSD"]}`
And the InstrumentDefinition registry has rows for AAPL (NASDAQ), ES (CME), EURUSD (IDEALPRO)
When I run a backtest via /api/v1/backtests/run
Then the backtest loads Nautilus Instrument objects with IDs AAPL.NASDAQ, ESM6.CME (front-month), EURUSD.IDEALPRO
And the catalog-builder resolves instruments via SecurityMaster.resolve_for_backtest
When I deploy the same strategy to a live IB paper account
Then the live TradingNode loads Nautilus Instrument objects with IDs AAPL.NASDAQ, ESM6.CME, EURUSD.IDEALPRO
And the strategy code is byte-identical between backtest and live
```

**Acceptance Criteria:**

- [ ] `StrategyConfig.instruments` accepts plain symbols without venue suffix.
- [ ] `SecurityMaster.resolve_for_live(["AAPL"])` returns `[InstrumentId("AAPL.NASDAQ")]` via async control-plane lookup.
- [ ] `SecurityMaster.resolve_for_backtest(["AAPL"])` returns `[InstrumentId("AAPL.NASDAQ")]` — same as live.
- [ ] `SecurityMaster.resolve_for_live(["ES"])` returns front-month `InstrumentId("ESM6.CME")` via existing `canonical_instrument_id(today=...)` roll logic.
- [ ] `SecurityMaster.resolve_for_backtest(["ES.Z.5"])` returns a synthetic continuous `InstrumentId("ES.Z.5.CME")` hydrated from Databento definition data.
- [ ] A parity integration test deploys `MyStrategy` to backtest and live-paper for AAPL + ES, asserts identical `InstrumentId` strings and identical `Instrument` price-precision/multiplier fields.

**Edge Cases:**

| Condition                                                 | Expected Behavior                                                                                                                               |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Unknown symbol (`"ZZZZ"`) on live path                    | Async IB qualify attempts; if IB rejects, raises `InstrumentUnknown` → `/live/start` returns 422                                                |
| Unknown symbol on backtest path                           | Raises `InstrumentDefinitionMissing(raw_symbol=...)`. Caller (backtest API) returns 422 with hint "run msai instruments refresh --symbols ZZZZ" |
| Symbol already in registry with `.XCME` legacy alias      | Resolves to the `.CME` canonical alias (defensive input normalization preserved)                                                                |
| Symbol has ambiguous venue (dual-listed equity)           | Registry query returns `AmbiguousSymbol`; strategy must pass dotted ID (`AAPL.NASDAQ`) explicitly                                               |
| Continuous-futures pattern (`ES.Z.5`) passed to live path | Rejected — live path only accepts IB-form symbols (`ES` or `ESM6.CME`)                                                                          |

**Priority:** Must Have

---

### US-002: Warm-cache sync resolution in the strategy hot path

**As a** strategy author running a production live deployment
**I want** instrument resolution to be synchronous dictionary lookup inside `Strategy.on_bar` / `on_quote_tick` / `on_trade_tick`
**So that** the trading loop never blocks on DB or network I/O

**Scenario:**

```gherkin
Given a live deployment is running with pre-loaded instruments
When a bar arrives on the MessageBus
Then Strategy.on_bar resolves the instrument via Nautilus's own cache.instrument(instrument_id) — sync dict lookup
And no SQLAlchemy session is created inside the hot path
And no IB round-trip is made inside the hot path
```

**Acceptance Criteria:**

- [ ] `SecurityMaster` has two sync vs async surfaces: async `resolve_for_live/backtest` (DB + provider, called during deployment bootstrap), sync `find(instrument_id)` (Nautilus cache lookup, called during trading).
- [ ] `live_supervisor.TradingNodePayload` carries the resolved `InstrumentId`s pre-computed by the FastAPI `/live/start-portfolio` handler. Subprocess only needs sync cache warmup.
- [ ] Unit test: grep `Strategy.on_bar` call sites for `async` / `await` / `session.execute` / `session.get` — must be zero in MSAI-authored strategies.

**Priority:** Must Have

---

### US-003: Operator reads logs and DB without mental translation

**As** Pablo the operator
**I want** log lines and UI labels to show `AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO` (exchange-name, matches IB TWS display)
**So that** I can verify any fill against IB TWS instantly at 3am without translating MIC codes

**Scenario:**

```gherkin
Given a live paper deployment of AAPL strategy
When a BUY order fills
Then the docker logs line reads: "events.fills.AAPL.NASDAQ — Filled BUY 1 @ 261.33"
And the live positions UI table renders the instrument_id cell as "AAPL.NASDAQ"
And an operator running docker compose logs | grep "AAPL.NASDAQ" finds all relevant events
And an operator opening IB TWS sees AAPL on NASDAQ — same readable string
```

**Acceptance Criteria:**

- [ ] Every log line emitted by `live_supervisor`, `execution.engine`, and custom MSAI services uses exchange-name format for the venue portion of `InstrumentId`.
- [ ] Frontend `live/positions-table.tsx`, `dashboard/recent-trades.tsx`, `backtests/trade-log.tsx` render `instrument_id` strings verbatim — no MIC translation.
- [ ] `msai live-status` CLI output uses exchange-name format.
- [ ] DB query `SELECT * FROM live_deployment_strategies WHERE instruments @> ARRAY['AAPL.NASDAQ']` returns matching rows; no alternate MIC-form rows confuse the query.

**Edge Cases:**

| Condition                                           | Expected Behavior                                                                                                                                        |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Historical rows use mixed `.XCME` / `.CME` format   | One-time migration normalizes to `.CME`; deprecated format rejected on new input except explicit legacy-accept path (`live_instrument_bootstrap.py:147`) |
| Future options instrument (`AAPL_250117C150.SMART`) | Routing venue = `SMART`; logs + UI show `SMART`; risk/analytics queries use `listing_venue` column from registry (CBOE/PHLX/ISE)                         |

**Priority:** Must Have

---

### US-004: Risk/analytics queries leverage listing-venue split

**As a** risk engineer (Pablo in risk-analysis mode)
**I want** the registry to store `listing_venue` separately from `routing_venue`
**So that** I can query "all options listed on CBOE" or "margin rules per listing exchange" without parsing Nautilus `InstrumentId` strings

**Scenario:**

```gherkin
Given the registry has equity AAPL (listing=NASDAQ, routing=NASDAQ)
And has future ES (listing=CME, routing=CME)
And has FX EURUSD (listing=IDEALPRO, routing=IDEALPRO)
And will later have option AAPL_250117C150 (listing=CBOE, routing=SMART)
When I query SELECT * FROM instrument_definition WHERE listing_venue = 'CBOE'
Then the query returns all options listed on CBOE regardless of how IB routed the execution
```

**Acceptance Criteria:**

- [ ] `InstrumentDefinition` table has BOTH `listing_venue` and `routing_venue` columns from day one.
- [ ] For equities/futures/FX today: `listing_venue` = `routing_venue` (populated identically).
- [ ] Integration test creates one mock option row with `listing_venue='CBOE'`, `routing_venue='SMART'` to prove the schema supports the split.
- [ ] Registry population code extracts `listing_venue` from Nautilus's `contract_details.info['contract']['primaryExchange']` at register time (IB adapter preserves this).

**Priority:** Must Have (schema); Nice to Have (populate non-trivially — only options need the split today, and options are future work)

---

### US-005: Continuous-futures backtest via Databento `.Z.N` pattern

**As a** strategy author
**I want** to backtest a strategy on Databento continuous-futures data (e.g. `ES.Z.5` = 5th forward-month continuous for ES)
**So that** I can evaluate strategies on a roll-adjusted price series matching what live ES will deliver

**Scenario:**

```gherkin
Given a Databento definition file at ${DATABENTO_DEFINITION_ROOT}/GLBX.MDP3/ES.Z.5/2024-01-01_2024-12-31.definition.dbn.zst
And an InstrumentDefinition row for raw_symbol='ES', provider='databento', continuous_pattern='.Z.5'
When I run a backtest with strategy.instruments=["ES.Z.5"] and date range 2024-01-01..2024-12-31
Then SecurityMaster.resolve_for_backtest recognizes the .Z.N pattern
And returns a synthetic Nautilus Instrument with instrument_id="ES.Z.5.CME" whose activation_ns/expiration_ns span the request window
And the backtest loads this instrument into the ParquetDataCatalog and runs
```

**Acceptance Criteria:**

- [ ] Port `_DATABENTO_CONTINUOUS_SYMBOL` regex (`^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$`) from codex-version at `instrument_service.py:440`.
- [ ] Port `_raw_symbol_from_request` + `_resolved_databento_definition` + `_continuous_definition_needs_refresh` + `_definition_window_bounds` helpers — verbatim logic, adapted names as needed.
- [ ] `DatabentoClient` gains `fetch_definition_instruments(raw_symbol, start, end, dataset, target_path)` method OR reuses existing client method if one exists.
- [ ] Integration test with a real Databento definition fixture produces bars in a backtest.

**Edge Cases:**

| Condition                                                        | Expected Behavior                                                                                              |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `.Z.N` pattern requested for a live deployment                   | Rejected — live only accepts IB `ES` or concrete `ESM6.CME`                                                    |
| Requested continuous window falls outside existing cached window | Service extends the window and re-fetches from Databento; updates `effective_from`/`effective_to` on alias row |
| Databento returns no instruments for the requested symbol        | Raises `DatabentoDefinitionEmpty(raw_symbol, start, end)`; backtest fails with operator hint                   |
| `force_refresh=True` is passed and Databento is unreachable      | Raises `DatabentoUnreachable` (fail loud — backtest is batch, operator reruns)                                 |

**Priority:** Must Have

---

### US-006: Nautilus owns `Instrument` payload durability; MSAI owns control-plane metadata

**As a** system architect
**I want** Nautilus's own cache DB backend (`CacheConfig(database=redis)`) to hold serialized `Instrument` payloads
**So that** MSAI does not duplicate what Nautilus already provides, and `Instrument` payload upgrades (Nautilus version bumps, msgpack schema changes) work through Nautilus's own code path

**Scenario:**

```gherkin
Given MSAI is configured with CacheConfig(database=redis)
And claude-version's live TradingNode subprocess restarts
When Nautilus cache loads instruments from Redis on boot
Then the Instrument objects are fully hydrated without consulting MSAI's Postgres table
And MSAI's InstrumentDefinition table contains NO serialized Instrument payload — only raw_symbol, listing_venue, routing_venue, provider, roll_policy, refreshed_at, lifecycle_state
```

**Acceptance Criteria:**

- [ ] `InstrumentDefinition` SQLAlchemy model does NOT have an `instrument_data` JSONB column (counter-example: codex-version has one; MSAI explicitly does not).
- [ ] MSAI's live compose config wires `CacheConfig(database=redis)` with the existing Redis service (nautilus.md gotcha #7).
- [ ] Integration test: deploy a strategy, trigger a fill, kill the subprocess, restart — verify Nautilus loads the `Instrument` from Redis without DB lookup.

**Priority:** Must Have

---

### US-007: Split-brain normalization (`.XCME` → `.CME`) bundled in PR

**As a** future maintainer of this codebase
**I want** one consistent venue-string format across claude-version source, tests, and documentation
**So that** I never need to grep for both `ES.CME` AND `ES.XCME` to understand what's happening

**Scenario:**

```gherkin
Given claude-version currently uses .CME at runtime but .XCME survives in 7 source-file docstrings/examples, 26 test fixtures, and the security_master/specs.py canonical format doc
When this PR merges
Then grep -rn "\.XCME" claude-version/backend/ returns only one occurrence: the legacy-input-accept comment at live_instrument_bootstrap.py:147
And canonical docs in security_master/specs.py show ES.CME / ESM5.CME (not ES.XCME / ESM5.XCME)
And all test fixtures use .CME format
And a Nautilus cache re-warm on first boot repopulates cache keys under .CME format
```

**Acceptance Criteria:**

- [ ] 7 source-file docstring/example updates: `instrument_cache.py:4`, `api/backtests.py:84`, `live_instrument_bootstrap.py:82`, `security_master/specs.py:21-22`, `services/nautilus/instruments.py:63`, `backtest_runner.py:75`, plus any surfaced during implementation.
- [ ] 26 test-fixture updates under `claude-version/backend/tests/`.
- [ ] `live_instrument_bootstrap.py:147` retains legacy-input-accept but canonical docstring says `.CME` only.
- [ ] No DB migration needed for live rows (docker stack not up to quantify; best-effort check during implementation).
- [ ] No Parquet disk rewrite (MSAI stores by symbol, not venue).

**Priority:** Must Have (bundled per user decision)

---

### US-008: Strategy config schema extraction for future UI consumption

**As a** future frontend engineer (or Pablo configuring a strategy via UI)
**I want** `GET /api/v1/strategies/{id}` to include the strategy's Pydantic config JSON-schema + defaults
**So that** a UI form generator can render appropriate inputs per strategy without hardcoding every config shape

**Scenario:**

```gherkin
Given a strategy MyStrategy(Strategy) with a MyStrategyConfig(BaseModel) class defining fast_period: int = 10, slow_period: int = 20
When I GET /api/v1/strategies/{my_strategy_id}
Then the response includes config_schema = MyStrategyConfig.model_json_schema()
And includes config_defaults = {"fast_period": 10, "slow_period": 20}
And a frontend can render a form from config_schema + pre-populate from config_defaults
```

**Acceptance Criteria:**

- [ ] `StrategyRegistry.scan()` inspects each strategy file, locates its `Config` Pydantic class, and captures `model_json_schema()` + per-field defaults.
- [ ] `GET /api/v1/strategies/{id}` response includes new fields: `config_schema: dict`, `config_defaults: dict`.
- [ ] `GET /api/v1/strategies/` list response also includes these fields for each strategy.
- [ ] Unit test: a fixture strategy with known config produces expected `config_schema` structure.

**Priority:** Must Have (per user's Q1 scope decision — no defer)

---

## 5. Technical Constraints

### Known Limitations

- **NautilusTrader's `InstrumentId.venue` is a single string** — no native listing/routing split. MSAI compensates with separate columns, populated from `contract_details.info['contract']['primaryExchange']`.
- **Databento Python adapter has no continuous-symbol normalization** (verified: zero hits for `continuous|\.c\.0|\.Z\.` in the adapter). The `.Z.N` pattern is a real gap MSAI must fill.
- **Nautilus cache DB keys include the venue string** (`cache/database.pyx:583` — `f"{_INSTRUMENTS}:{instrument_id.to_str()}"`). Changing venue format invalidates cache entries — safe because Nautilus re-resolves via provider on miss.
- **IB options route via `SMART` by default** — listing exchange is dropped from `InstrumentId`. Preserved in `contract_details.info`. Registry must extract at register-time.
- **MSAI's raw Parquet storage is partitioned by symbol, not venue** — venue format change does NOT require disk rewrite.

### Dependencies

- **Requires:**
  - NautilusTrader 1.223.0 (already pinned) — `convert_exchange_to_mic_venue`, `use_exchange_as_venue` config flags, `ParquetDataCatalog.write_data(Instrument)`, `CacheConfig(database=redis)` support.
  - Redis 7 (already running in dev + prod compose) — used by Nautilus as cache DB backend.
  - PostgreSQL 16 (already in stack) — holds `InstrumentDefinition` + `instrument_alias` tables.
  - Databento Python client (already wired) — extended with `fetch_definition_instruments()` if not already present.
  - IB Gateway (already in stack) — source of truth for live-path contract qualification.
- **Blocked by:** nothing. All prior portfolio-per-account-live PRs (#29, #30, #31) have merged.

### Integration Points

- **NautilusTrader `InstrumentProvider`:** MSAI's `SecurityMaster` invokes the existing `InteractiveBrokersInstrumentProvider` + `DatabentoInstrumentProvider`. No subclassing; MSAI wraps them.
- **NautilusTrader `Cache`:** MSAI writes resolved `Instrument` objects via `provider.add_bulk()` at deployment start; Nautilus's cache DB handles durability via `CacheConfig(database=redis)`.
- **NautilusTrader `ParquetDataCatalog`:** MSAI's `catalog_builder` writes `Instrument` objects alongside bars (`write_data()` already supports this). Backtest loads via catalog hydration.
- **IB Gateway:** async qualification path for unknown symbols on `/live/start-portfolio`. Pre-warmed via `load_ids=[...]` in `InteractiveBrokersInstrumentProviderConfig`.
- **Databento:** `DatabentoClient.fetch_definition_instruments()` pulls definition data files into `${DATABENTO_DEFINITION_ROOT}/${dataset}/${raw_symbol}/${start}_${end}.definition.dbn.zst` (pattern ported from codex-version).

## 6. Data Requirements

### New Data Models

**`instrument_definition`** — control-plane primary key table.

| Column               | Type             | Notes                                                                                    |
| -------------------- | ---------------- | ---------------------------------------------------------------------------------------- |
| `instrument_uid`     | UUID PK          | Stable logical ID, NEVER venue-string.                                                   |
| `raw_symbol`         | VARCHAR(100)     | e.g. `AAPL`, `ES`, `EURUSD`. Indexed.                                                    |
| `listing_venue`      | VARCHAR(32)      | e.g. `NASDAQ`, `CME`, `IDEALPRO`, future `CBOE`/`PHLX`/`ISE` for options. Indexed.       |
| `routing_venue`      | VARCHAR(32)      | e.g. `NASDAQ`, `CME`, `IDEALPRO`, future `SMART` for options. Indexed.                   |
| `asset_class`        | VARCHAR(32)      | `equity`, `futures`, `fx`, `option`, `crypto`.                                           |
| `provider`           | VARCHAR(32)      | `interactive_brokers`, `databento`, `ibkr_smart`.                                        |
| `roll_policy`        | VARCHAR(64) NULL | e.g. `third_friday_quarterly` for ES/NQ; NULL for equities/FX.                           |
| `continuous_pattern` | VARCHAR(32) NULL | e.g. `.Z.5` for Databento 5th-forward-continuous. NULL for concrete contracts.           |
| `refreshed_at`       | TIMESTAMP        | Last time IB/Databento confirmed the definition. Staleness = `now - refreshed_at > 24h`. |
| `lifecycle_state`    | VARCHAR(32)      | `staged`, `active`, `retired`.                                                           |
| `created_at`         | TIMESTAMP        | Auto — `TimestampMixin`.                                                                 |
| `updated_at`         | TIMESTAMP        | Auto — `TimestampMixin`.                                                                 |

Constraints: unique `(raw_symbol, provider, asset_class)` — one logical definition per symbol per provider per asset class.

**`instrument_alias`** — maps venue-qualified `InstrumentId` strings to the logical UID.

| Column           | Type         | Notes                                                                                           |
| ---------------- | ------------ | ----------------------------------------------------------------------------------------------- |
| `id`             | UUID PK      | Row ID.                                                                                         |
| `instrument_uid` | UUID FK      | → `instrument_definition.instrument_uid`. Indexed.                                              |
| `alias_string`   | VARCHAR(100) | e.g. `AAPL.NASDAQ`, `ESM6.CME`, `ES.Z.5.CME`, `EURUSD.IDEALPRO`, `AAPL.XCME` (legacy). Indexed. |
| `venue_format`   | VARCHAR(16)  | `exchange_name`, `mic_code`, `databento_continuous`.                                            |
| `provider`       | VARCHAR(32)  | Which data provider uses this alias. Mirrors `instrument_definition.provider` for consistency.  |
| `effective_from` | DATE         | When this alias became valid (for futures front-month rolls: the roll date).                    |
| `effective_to`   | DATE NULL    | NULL = still current. Set on roll or retirement.                                                |
| `created_at`     | TIMESTAMP    | Auto.                                                                                           |

Constraints: unique `(alias_string, provider, effective_from)` — one alias per string per provider per effective date.

### Data Validation Rules

- `raw_symbol`: `[A-Za-z0-9/_-]+`, not empty, max 100 chars.
- `listing_venue` + `routing_venue`: alphanumeric, max 32 chars (accommodates `XCME`, `IDEALPRO`, `OPRA`, etc).
- `asset_class`: enum `{equity, futures, fx, option, crypto}`.
- `lifecycle_state`: enum `{staged, active, retired}`.
- `continuous_pattern`: if not NULL, must match regex `^\.[A-Za-z]\.\d+$`.
- `alias_string`: must contain exactly one `.` separator for venue-qualified IDs (except `.Z.N` which has two); validated per `venue_format`.

### Data Migration

- **Alembic migration**: create the two tables. Empty at ship.
- **Seed rows**: static control-plane seeds for known continuous-futures symbols (`ES`, `NQ`, `RTY`, `YM` with `.Z.5` patterns and `third_friday_quarterly` roll policy). Static metadata — no network calls during migration.
- **No backfill** of existing `strategy.instruments` arrays from live deployments; those already carry canonical exchange-name strings that the registry will encounter lazily on next `/live/start`.
- **Split-brain normalization**: a separate idempotent one-shot script (`scripts/normalize_venue_strings.py`) runs as part of migration OR can be invoked independently. Updates any `live_deployment_strategies` rows containing `.XCME` → `.CME`. Dry-run mode prints planned changes.

## 7. Security Considerations

- **Authentication:** `/api/v1/strategies/*` and `/api/v1/instruments/*` (if added) require Azure Entra ID JWT (existing middleware).
- **Authorization:** single-user platform; Pablo has full access. No RBAC matrix.
- **Data Protection:**
  - `InstrumentDefinition` contains no PII and no secrets.
  - Databento definition files on disk are plain market data — not sensitive.
  - IB contract details may include account-related metadata in `contract_details.info`; sanitize before logging (already filtered in existing logging middleware).
- **Audit:**
  - Every `instrument_definition` row creation is logged by existing `audit_log` middleware.
  - `msai instruments refresh` CLI invocations log `who ran, when, which symbols, which provider, outcome`.
- **Input validation:**
  - `raw_symbol` regex validated at API boundary.
  - `.Z.N` continuous-pattern regex validated.
  - Venue strings constrained to alphanumeric — protects against injection in LIKE queries.
- **Rate limiting:** existing FastAPI rate limits apply. `/api/v1/strategies/` schema extraction should be cached (schema is deterministic per strategy code).

## 8. Open Questions

> Items flagged during discussion that will be resolved during implementation, not before.

- [ ] Quantify the count of mixed-format rows in live `live_deployment_strategy` (need docker stack up to query). Affects normalization script dry-run output but not schema.
- [ ] Verify `Databento loader use_exchange_as_venue=True` emits `CME`/`NYMEX`/`CBOT` correctly in MSAI's ingestion path end-to-end (spike < 30 min during implementation).
- [ ] Verify Nautilus's cache DB fully subsumes codex-version's msgpack `instrument_data` JSONB need (strong hypothesis: yes; confirm with integration test).
- [ ] Decide whether to deprecate the two existing `canonical_instrument_id` functions (in `services/nautilus/instruments.py` and `services/nautilus/live_instrument_bootstrap.py`) or keep them as thin wrappers around `SecurityMaster`.
- [ ] Decide exact wiring in `catalog_builder.py` — write `Instrument` alongside bars on each build, or lazily on first read?
- [ ] Placement of `SecurityMaster` — `services/nautilus/security_master/service.py` (an existing directory in claude-version) or new top-level `services/instrument_registry/`? Hypothesis: extend `security_master/`.

## 9. References

- **Discussion log:** `docs/prds/db-backed-strategy-registry-discussion.md`
- **Council verdict:** `/tmp/msai-research/council/chairman-verdict.md`
- **Individual advisor outputs:** `/tmp/msai-research/council/advisor-{maintainer,nautilus-architect,ux-operator,contrarian,data-engineer}.md`
- **Research outputs:**
  - Claude Explore agent: synthesized in discussion log "Research Streams" section
  - Codex CLI research: `/tmp/msai-research/codex-research.md`
- **Related plan docs:**
  - `docs/plans/2026-04-13-codex-claude-subsystem-audit.md §2` — original port-scoping audit
  - `docs/plans/2026-02-25-msai-v2-implementation.md` — MSAI Phase 2 placement
  - `docs/plans/2026-04-16-portfolio-per-account-live-design.md` — adjacent live-composition design
- **Port source:** `codex-version/backend/src/msai/services/nautilus/instrument_service.py` (lines 32–106, 440–605 for the `.Z.N` helpers) and `codex-version/backend/src/msai/models/instrument_definition.py` (reference schema, adapted for UUID PK)
- **Nautilus venv (verified facts):**
  - `common/providers.py:28-62` — `InstrumentProvider` base (in-memory dict, no built-in persistence)
  - `common/providers.py:364-380` — `find()` sync warm-cache lookup
  - `adapters/interactive_brokers/parsing/instruments.py:319` — `FUT` + `CONTFUT` both parse to `FuturesContract`
  - `adapters/interactive_brokers/config.py:128,190` — `convert_exchange_to_mic_venue=False` default
  - `adapters/databento/loaders.py:123-157` — `use_exchange_as_venue` toggle; default False emits `GLBX`
  - `persistence/catalog/parquet.py:294-299` — `ParquetDataCatalog.write_data()` treats `Instrument` as first-class
  - `cache/database.pyx:340-377,583,934-950` — Cache DB `load_instrument`/`load_instruments` under key `f"{_INSTRUMENTS}:{instrument_id.to_str()}"`
  - `execution/engine.pyx:851-889,1304-1323,1620-1623` — MessageBus topics `events.order.*`, `events.fills.*`, `events.position.*`
- **MSAI CLAUDE.md gotchas referenced:** #7 (CacheConfig with DB backend for restart cache rehydration), #9 (pre-load all instruments at startup), #11 (dynamic instrument loading is synchronous and slow — don't do it in hot path).

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                                                                                                                                                                                              |
| ------- | ---------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-04-17 | Claude + Pablo | Initial PRD. Council verdict: hybrid (third option at schema + Option B at runtime alias) accepted by user. Missing-evidence items researched and resolved. Scope bundles split-brain normalization + Pydantic schema extraction per user decisions. |

## Appendix B: Approval

- [ ] Pablo (Product + Tech Lead) approval
- [ ] Ready for technical design (Phase 3.1 brainstorming may be skipped per `/new-feature` workflow since council already ran; proceed directly to Phase 3.2 `/superpowers:writing-plans`)
