# PRD: Live-path wiring onto instrument registry

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-19
**Last Updated:** 2026-04-19

---

## 1. Overview

MSAI's live-start path (`POST /api/v1/live/start-portfolio` → `live_supervisor` → trading subprocess) currently resolves instrument IDs through `canonical_instrument_id()` — a hardcoded 5-symbol if/elif chain (AAPL, MSFT, SPY, EUR/USD, ES). The DB-backed instrument registry (`instrument_definitions` + `instrument_aliases`) and the `msai instruments refresh --provider interactive_brokers` CLI that populates it both ship, but the registry is **write-only** from live-start's perspective: operators can warm it, but live deploys still reject symbols not in the hardcoded chain. This PRD eliminates that asymmetry by introducing a pure-read `lookup_for_live(symbols, as_of_date)` API and wiring the three live-start resolution sites (supervisor, IB provider config builder, live node config) onto it — enabling live trading of any equity, index ETF, forex pair, or future IB can qualify, with no code edits or redeploys per new symbol.

## 2. Goals & Success Metrics

### Goals

- **Unblock the live-start critical path for any IB-qualifiable symbol.** Trading a new symbol becomes `msai instruments refresh --symbols X` + deploy, not "edit `canonical_instrument_id()` + redeploy."
- **Make the registry the single source of truth for live-start instrument resolution.** No silent fallbacks to the hardcoded chain. No IB Gateway round-trips on the live-start critical path (those stay confined to the `instruments refresh` CLI).
- **Design for options extensibility.** The `lookup_for_live` API contract must accommodate option specs (expiry + strike + call/put) as a future payload variant without runtime-contract breakage when options trading ships later.
- **Fix the existing timezone mismatch** between Chicago-local futures-roll logic and the registry's implicit UTC alias-windowing — reconcile by threading `spawn_today` explicitly in `America/Chicago` through the resolver.

### Success Metrics

| Metric                                                    | Target                              | How Measured                                                                                                                                                                                                |
| --------------------------------------------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Non-Phase-1 symbol deployable live                        | Any 1 of QQQ / GBP/USD / NQ / GOOGL | Real-money drill on IB live account U4705114 with 1-share BUY → `/kill-all` → position flat, `trades` row written correctly                                                                                 |
| `canonical_instrument_id` removed from live-start runtime | 0 hits                              | `grep "canonical_instrument_id(" backend/src/msai/live_supervisor/ backend/src/msai/services/nautilus/live_instrument_bootstrap.py` returns zero outside the helper definition itself and CLI seeding paths |
| Resolution source is registry                             | ≥99% in drill logs                  | Structured log `live_instrument_resolved{source="registry"}` on every deploy; no `source="canonical"` hits in post-merge paper week                                                                         |
| Drill completion cost                                     | <$5                                 | Sum of slippage + commissions on the drill trade on U4705114                                                                                                                                                |
| Test coverage of new code                                 | 100% of `lookup_for_live` branches  | `pytest --cov=msai.services.nautilus.security_master` reports full coverage on the new resolver                                                                                                             |
| Merge gate — drill passes BEFORE merge                    | PASS                                | Drill report documents all 8 A3 checklist items (see `live-path-wiring-registry-discussion.md`)                                                                                                             |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Options trading** — deferred to a separate PRD + council. This PR's resolver contract must be options-ready, but options wiring itself is not in scope.
- ❌ **HTTP preflight layer** (Option C from the council) — resolution happens at spawn, not at API handler. Can be added later as an advisory guardrail.
- ❌ **UI for registry management** — operators continue to use the `msai instruments refresh` CLI for now. A UI is part of the follow-up Symbol Onboarding feature (#3b in `CONTINUITY.md`).
- ❌ **Automatic registry warming** — registry remains operator-managed. No background job that discovers "users are deploying X" and auto-refreshes.
- ❌ **Ingestion coverage audit across all 4 asset classes** — deferred to Symbol Onboarding (#3b). This PR assumes operators run `msai ingest` to populate historical data before deploying. FX/ETF ingestion-parity is a separate workstream.
- ❌ **Dashboards / Grafana / alert wiring** — structured logs + Prometheus counter ship; visualization is deferred to CI-hardening or ops follow-up.
- ❌ **Deleting `canonical_instrument_id()`** — it stays in CLI/bootstrap seeding paths (still used by `msai instruments refresh` for initial 5-symbol population). Only the **live-start runtime** references are removed.
- ❌ **`instrument_cache` → registry migration** (#4 in CONTINUITY `Next`) — legacy cache stays until a dedicated PR handles it.
- ❌ **Crypto instruments** — IB's Paxos integration is separate work; not addressed here.

## 3. User Personas

### Operator (Pablo, sole persona)

- **Role:** Manager of the personal hedge fund. Writes strategies in Python, runs backtests, deploys to paper + live.
- **Permissions:** Full access via API + CLI + UI. Can pre-warm registry, create portfolios, freeze revisions, deploy, `/kill-all`.
- **Goals:** Trade a diverse symbol universe (any equity, ETF, FX pair, future) without hand-editing Python code or waiting on redeploys per symbol. Fail fast on configuration gaps with clear operator hints.
- **Technical comfort:** High — uses CLI daily, reads docker logs, writes SQL ad-hoc for diagnostics.

No other personas in this PR (no analysts, no external API consumers, no multi-tenant end-users).

## 4. User Stories

### US-001: Deploy any IB-qualifiable symbol without code edits

**As an** operator
**I want** to trade any IB-qualifiable symbol by pre-warming the registry via CLI
**So that** I'm not blocked on code edits / redeploys per new symbol

**Scenario:**

```gherkin
Given the registry does NOT contain QQQ
And I run `msai instruments refresh --symbols QQQ --provider interactive_brokers`
And the command succeeds and writes instrument_definitions + instrument_aliases rows for QQQ
When I create a portfolio revision containing QQQ
And I POST /api/v1/live/start-portfolio for that revision
Then the live_supervisor's lookup_for_live returns QQQ's canonical InstrumentId
And the trading subprocess preloads QQQ's contract spec from the same registry row
And IB Gateway subscribes to QQQ bars
And the strategy receives bar events within 60 seconds of deploy
```

**Acceptance Criteria:**

- [ ] `lookup_for_live(["QQQ"], as_of_date=today())` returns `[ResolvedInstrument(canonical_id="QQQ.NASDAQ", asset_class="equity", contract_spec=...)]` after pre-warm
- [ ] `build_ib_instrument_provider_config` is wired to `lookup_for_live` (no more `PHASE_1_PAPER_SYMBOLS` gate)
- [ ] `live_node_config.py:478` passes resolver output to provider config, not raw user symbols
- [ ] Deploy succeeds end-to-end on IB paper (`DUP733211`) for at least one symbol per non-Phase-1 asset class (QQQ = ETF, at minimum)
- [ ] No code changes required in `live_supervisor/__main__.py` to add symbol #6, #7, #N

**Edge Cases:**

| Condition                                                 | Expected Behavior                                                                                                 |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Symbol is in Phase-1 set (AAPL, MSFT, SPY, EUR/USD, ES)   | Still resolves via registry (no special-case; registry is pre-seeded via `msai instruments refresh` on repo init) |
| Symbol qualifies via IB but CLI hasn't been run           | Registry miss — see US-002                                                                                        |
| Symbol has effective-date-windowed aliases (futures roll) | Resolver returns alias active on `as_of_date` (`spawn_today` in America/Chicago)                                  |

**Priority:** Must Have

---

### US-002: Fail fast on registry miss with copy-pastable fix

**As an** operator
**I want** a clear error telling me which symbol is missing and how to add it when I try to deploy an un-warmed symbol
**So that** I can self-correct in seconds instead of debugging silent subscription failures

**Scenario:**

```gherkin
Given the registry does NOT contain GBP/USD
And I try to POST /api/v1/live/start-portfolio for a revision containing GBP/USD
When the live_supervisor invokes lookup_for_live(["GBP/USD"], spawn_today)
Then lookup_for_live raises RegistryMissError(symbols=["GBP/USD"])
And the supervisor short-circuits the spawn (no subprocess started, no IB call attempted)
And the API response is HTTP 422 with body:
  {
    "error": {
      "code": "REGISTRY_MISS",
      "message": "Symbol(s) not in registry: ['GBP/USD']. Run: msai instruments refresh --symbols GBP/USD --provider interactive_brokers",
      "details": {"missing_symbols": ["GBP/USD"]},
      "request_id": "req_..."
    }
  }
And alerting_service emits a WARN-level alert "Registry miss on deploy"
```

**Acceptance Criteria:**

- [ ] `lookup_for_live` raises a typed `RegistryMissError` on any symbol not found in `instrument_aliases` windowed by `as_of_date`
- [ ] `live_supervisor` spawn catches `RegistryMissError` and publishes failure reason to `LiveCommandBus` DLQ + updates `LiveDeployment.status=failed` with the error detail
- [ ] API handler maps the supervisor failure to HTTP 422 with the documented body shape
- [ ] Error message includes the EXACT `msai instruments refresh` command the operator can copy-paste
- [ ] `alerting_service.warn` fires on each registry miss (not ERROR — operator-recoverable)
- [ ] No IB Gateway network call happens on the registry-miss path (verified by unit test using `page.route`-equivalent mocking or an IB client stub)

**Edge Cases:**

| Condition                                                          | Expected Behavior                                                                                                             |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Multiple symbols in one deploy, only one is missing                | `RegistryMissError(symbols=["GBP/USD"])` lists only the missing ones; entire deploy fails (no partial spawn)                  |
| Registry has row but `effective_to` < `as_of_date` (alias expired) | Treated as miss; error message says "no active alias for X on YYYY-MM-DD"                                                     |
| Registry row exists but `asset_class` is `option`                  | This PR scopes to equity/ETF/FX/future — raise `UnsupportedAssetClassError` with "options not yet supported for live trading" |

**Priority:** Must Have

---

### US-003: Futures-roll safety at spawn

**As an** operator
**I want** the resolver and the subprocess to agree on the same front-month contract when I deploy on a futures-roll day
**So that** the strategy's bar subscription matches the contract IB actually trades, and I don't discover the mismatch at first order

**Scenario:**

```gherkin
Given the current exchange-local date in America/Chicago is the third Friday of June 2026 (ES roll day)
And the registry has alias rows for ES:
  - ESM6 (June 2026) with effective_to = 2026-06-19
  - ESU6 (September 2026) with effective_from = 2026-06-20
When the live supervisor computes spawn_today in America/Chicago
And spawn_today = 2026-06-20 (post-roll)
And it calls lookup_for_live(["ES"], as_of_date=spawn_today)
Then the resolver returns canonical_id="ESU6.CME" (September contract, not June)
And the subprocess preloads ESU6 via build_ib_instrument_provider_config
And IB Gateway subscribes to ESU6 bars
```

**Acceptance Criteria:**

- [ ] `lookup_for_live` accepts `as_of_date: datetime.date` as an explicit required parameter (no default to `date.today()`)
- [ ] Callers (supervisor, provider config, node config) pass `spawn_today` computed via `exchange_local_today()` in `America/Chicago` (per nautilus.md gotcha #3)
- [ ] Resolver queries `instrument_aliases` with `effective_from <= as_of_date AND (effective_to IS NULL OR effective_to > as_of_date)` — matches the existing `resolve_for_backtest` semantics (`backend/src/msai/services/nautilus/security_master/registry.py` must be adjusted if current `find_by_alias` defaults to UTC)
- [ ] Integration test: seed two overlapping ES alias rows (June + Sept); call `lookup_for_live(["ES"], as_of=2026-06-19)` → returns ESM6; call with `as_of=2026-06-20` → returns ESU6
- [ ] `canonical_instrument_id()`'s existing Chicago-local front-month calculation is NOT duplicated in the resolver — registry is authoritative

**Edge Cases:**

| Condition                                                                                       | Expected Behavior                                                                                                                                                                                                        |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Spawn crosses midnight CT on roll day                                                           | `spawn_today` is computed once at supervisor spawn start (per existing design, see `live_supervisor/__main__.py:119-126`) and threaded through the entire spawn lifecycle. No mid-spawn recomputation.                   |
| Registry has no aliases for a futures root symbol (e.g., operator forgot to refresh after roll) | Treated as registry miss per US-002. Error message specific: "No active alias for ES on 2026-06-20. The most recent alias expired 2026-03-19. Run: msai instruments refresh --symbols ES --provider interactive_brokers" |
| Two aliases overlap in effective window (operator error)                                        | Resolver picks the one with the most recent `effective_from`; logs a WARN so operator can clean up the registry state                                                                                                    |

**Priority:** Must Have

---

### US-004: Options-ready resolver contract

**As a** future operator (post-options-PRD)
**I want** the options-trading feature to slot in without re-architecting the resolver
**So that** today's equity/ETF/FX/future wiring isn't wasted work when we ship options

**Scenario:**

```gherkin
Given the resolver signature is lookup_for_live(symbols, as_of_date) -> list[ResolvedInstrument]
And ResolvedInstrument is a discriminated union / protocol supporting equity, ETF, FX, and future today
When a future PRD adds options trading
Then it adds a new ResolvedInstrument variant for OptionSpec(expiry, strike, right)
And the supervisor + build_ib_instrument_provider_config pass ResolvedInstrument values through without caring about the variant — the IB adapter handles the specifics
And no existing callers of lookup_for_live need to change
```

**Acceptance Criteria:**

- [ ] `ResolvedInstrument` is typed as a dataclass or Pydantic model with explicit fields covering all 4 current asset classes (equity, ETF, FX, future)
- [ ] Fields include `canonical_id: str`, `asset_class: AssetClass` (enum), `contract_spec: dict[str, Any]` (IB-compatible), `effective_window: (date, date | None)`
- [ ] `contract_spec` is opaque to the supervisor — it's a blob passed to the IB provider config. The IB adapter parses it, not the supervisor. This is what lets options slot in later.
- [ ] A design note in the resolver module's docstring explicitly states: "Extending to options requires adding `OptionSpec` fields (expiry, strike, right) to contract_spec; the supervisor + resolver signatures do NOT change."
- [ ] Unit test: construct a `ResolvedInstrument` with a fabricated option-like `contract_spec`; verify the resolver's return shape accepts it (the IB adapter would then fail if called, but that's expected — we're not wiring options, just proving the contract is shape-extensible)

**Edge Cases:**

| Condition                               | Expected Behavior                                                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Someone tries to deploy an option today | `lookup_for_live` returns ResolvedInstrument with `asset_class="option"` → supervisor raises `UnsupportedAssetClassError` (per US-002 edge case) |

**Priority:** Should Have (design constraint, not runtime feature)

---

### US-005: Observability for resolution source

**As an** operator
**I want** every live-start resolution emitted as a structured log + Prometheus counter
**So that** I can confirm the live-path is actually using the registry (not a hidden fallback) post-deploy

**Scenario:**

```gherkin
Given I deploy a portfolio containing QQQ and GOOGL
When the live_supervisor calls lookup_for_live(["QQQ", "GOOGL"], spawn_today)
And both resolve successfully from the registry
Then structured log entries are emitted:
  {level: "info", event: "live_instrument_resolved",
   source: "registry", symbol: "QQQ", canonical_id: "QQQ.NASDAQ",
   as_of_date: "2026-04-19", asset_class: "equity"}
  {level: "info", event: "live_instrument_resolved",
   source: "registry", symbol: "GOOGL", canonical_id: "GOOGL.NASDAQ",
   as_of_date: "2026-04-19", asset_class: "equity"}
And Prometheus counter msai_live_instrument_resolved_total{source="registry",asset_class="equity"} increments by 2
```

**Acceptance Criteria:**

- [ ] Every `lookup_for_live` resolution emits one structured log line per symbol via the project's existing `structlog`/`get_logger` pattern (see `backend/src/msai/core/logging.py`)
- [ ] Counter `msai_live_instrument_resolved_total` with labels `{source, asset_class}` is registered (either in existing `msai.services.observability.trading_metrics` or a new module if that file doesn't exist — plan-review will confirm)
- [ ] `source` label values: `"registry"` (success), `"registry_miss"` (US-002), `"registry_incomplete"` (US-006 corruption case)
- [ ] Log + counter fire BEFORE the supervisor spawns the subprocess — operator sees resolution success/failure ahead of any live market action
- [ ] Paper drill log grep: `jq 'select(.event=="live_instrument_resolved")'` on supervisor stdout shows one line per resolved symbol

**Edge Cases:**

| Condition                                  | Expected Behavior                                                                                     |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| High-frequency repeat deploys              | No counter/log rate-limiting — every resolution emits. Counter increments; structured logs are cheap. |
| Multi-strategy portfolio deploys N symbols | N log lines + N counter increments (one per symbol, not one per deploy)                               |

**Priority:** Must Have

---

### US-006: Hard fail on corrupt/partial registry row

**As an** operator
**I want** the resolver to hard-fail when a registry row is incomplete or corrupt
**So that** I catch data-integrity bugs at deploy time, not at first bar-subscription timeout

**Scenario:**

```gherkin
Given the registry has a row for NVDA where listing_venue IS NULL
And I try to deploy a portfolio containing NVDA
When lookup_for_live(["NVDA"], spawn_today) is called
Then it raises RegistryIncompleteError(symbol="NVDA", missing_field="listing_venue")
And the API returns HTTP 422 with code "REGISTRY_INCOMPLETE" and a descriptive message
And no subprocess is spawned
```

**Acceptance Criteria:**

- [ ] `lookup_for_live` validates every registry row against a required-fields checklist before returning
- [ ] Required fields for equity/ETF: `canonical_id`, `asset_class`, `listing_venue`, `routing_venue`
- [ ] Required fields for futures: above + `contract_month`, `expiry`, `underlying_root`
- [ ] Required fields for FX: above (non-futures) + `base_currency`, `quote_currency`, `exchange` (IDEALPRO)
- [ ] Typed exception `RegistryIncompleteError` distinct from `RegistryMissError`
- [ ] API handler maps to HTTP 422 with distinct error code `REGISTRY_INCOMPLETE`
- [ ] Alert fires at ERROR level (not WARN) — this is a data-integrity issue, not a user-correctable miss

**Edge Cases:**

| Condition                                                                           | Expected Behavior                                                                    |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| NULL field on a non-required column                                                 | Tolerated; resolver returns success                                                  |
| All required fields present but contract_spec is malformed (e.g., unparseable JSON) | Raised as `RegistryIncompleteError` with `missing_field="contract_spec.<json_path>"` |

**Priority:** Must Have

---

## 5. Technical Constraints

### Known Limitations

- **No IB qualifier on live-start critical path.** Cold-miss must be explicit operator action (`msai instruments refresh`), not an implicit network call. Council-mandated.
- **No silent `canonical_instrument_id` fallback.** Removed from `live_supervisor/` + `live_instrument_bootstrap.py`'s runtime path. Stays in CLI/bootstrap for initial seeding.
- **Registry miss = operator fix required.** The system cannot self-heal missing symbols; that's a deliberate design choice per the council's "operator-managed control plane" stance.
- **Chicago-local `as_of_date` contract.** Every caller of `lookup_for_live` must pass `spawn_today` computed via `exchange_local_today()` — naive UTC will regress roll-day behavior.

### Dependencies

- **Requires:** `instrument_definitions` + `instrument_aliases` schema (ships in PR #32); `msai instruments refresh --provider interactive_brokers` CLI (ships in PR #35). Both already on `main`.
- **Blocked by:** Nothing on `main`.
- **Blocks:** Symbol Onboarding UI/API feature (#3b in CONTINUITY `Next`) — that feature's design can start in parallel but its PR depends on this merging first.

### Integration Points

- **`InstrumentRegistry` (existing) at `backend/src/msai/services/nautilus/security_master/registry.py`** — provides `find_by_alias()` and `find_by_raw_symbol()` primitives. Current `find_by_alias` defaults to UTC; this PR must ensure Chicago-local `as_of_date` is respected (either pass-through parameter or internal conversion — plan-review will decide).
- **`LiveCommandBus` (existing) at `backend/src/msai/services/live_command_bus.py`** — supervisor publishes failure states here; new `REGISTRY_MISS` / `REGISTRY_INCOMPLETE` failure reasons integrate into existing DLQ pattern.
- **`trading_metrics` (may or may not exist) at `backend/src/msai/services/observability/`** — plan-review will confirm module exists; if not, create it.
- **`alerting_service` (existing)** — emits WARN + ERROR alerts to the file-backed audit trail consumed by `/api/v1/alerts/`.
- **Nautilus `InteractiveBrokersInstrumentProviderConfig`** — receives the resolved contract-spec blob. The IB adapter parses `contract_spec` back into `IBContract` objects; no changes to Nautilus required.

## 6. Data Requirements

### New Data Models

- **`ResolvedInstrument`** (dataclass or Pydantic model in `backend/src/msai/services/nautilus/security_master/`) — return type of `lookup_for_live`. Fields: `canonical_id: str`, `asset_class: AssetClass`, `contract_spec: dict`, `effective_window: tuple[date, date | None]`. Options-extensible.
- **`RegistryMissError`, `RegistryIncompleteError`, `UnsupportedAssetClassError`** — typed exceptions in the same module.

### Data Validation Rules

- `lookup_for_live(symbols=[], ...)` must raise `ValueError("symbols cannot be empty")` — fail-fast contract.
- `as_of_date` must be a `datetime.date` in Chicago-local time; no datetime-with-tz or naive UTC accepted. Enforce with a helper `validate_spawn_today(d: date) -> None`.
- Registry rows must match the required-fields checklist per asset class (see US-006).

### Data Migration

- **None required.** Schema is untouched; new resolver reads existing tables. Alembic-free PR.

## 7. Security Considerations

- **Authentication:** `/api/v1/live/start-portfolio` still requires Entra ID JWT (unchanged). The resolver itself is called inside the trusted supervisor process — no auth layer at the resolver boundary.
- **Authorization:** Any authenticated operator can deploy any symbol in the registry. This PR does not add row-level access control; the registry is shared across the single-operator deployment model.
- **Data Protection:** Registry rows contain no sensitive data (instrument metadata, not credentials). No PII.
- **Audit:** Every resolution emits `live_instrument_resolved` structured log → consumed by `alerting_service` → visible in `/api/v1/alerts/` audit trail. Registry misses + incompletes generate alerts at WARN/ERROR level.

## 8. Open Questions

> Questions that need answers before or during implementation. Resolve during plan-review.

- [ ] Does `backend/src/msai/services/observability/trading_metrics.py` already exist? If not, create it as part of this PR (~20 LOC).
- [ ] What enum values does `instrument_definitions.asset_class` support today? Match those for the Prometheus counter `asset_class` label.
- [ ] Does `alerting_service` support WARN level in addition to ERROR? If not, extend it (small change).
- [ ] Is there an FX pair currently ingested via `msai ingest`? If no, paper drill falls back to 2 equities (QQQ + GOOGL).
- [ ] Should `lookup_for_live` be `async` or sync? The registry reads are `AsyncSession`-based today, so async likely wins; confirm during plan-review.
- [ ] Where exactly does `spawn_today` enter `build_ib_instrument_provider_config`? Currently it's threaded via `TradingNodePayload.spawn_today_iso` — confirm the plumbing for the new resolver path.

## 9. References

- **Discussion Log:** [`docs/prds/live-path-wiring-registry-discussion.md`](./live-path-wiring-registry-discussion.md)
- **Decision Record:** [`docs/decisions/live-path-registry-wiring.md`](../decisions/live-path-registry-wiring.md)
- **Council Context:** [`docs/decisions/scratch/council-live-path-registry-wiring.md`](../decisions/scratch/council-live-path-registry-wiring.md)
- **Predecessor:** PR #32 (registry schema + `resolve_for_backtest`), PR #35 (`msai instruments refresh` IB path)
- **Related PRDs:**
  - [`docs/prds/db-backed-strategy-registry.md`](./db-backed-strategy-registry.md) — the schema this PR consumes
  - [`docs/prds/instruments-refresh-ib-path.md`](./instruments-refresh-ib-path.md) — the CLI that populates the registry
- **Follow-up feature:** Symbol Onboarding (#3b in `CONTINUITY.md` Next list)
- **NautilusTrader gotchas:** `.claude/rules/nautilus.md` — specifically #3 (IB client_id collisions), #4 (venue naming).

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                          |
| ------- | ---------- | -------------- | ---------------------------------------------------------------- |
| 1.0     | 2026-04-19 | Claude + Pablo | Initial PRD — derived from council verdict + discussion defaults |

## Appendix B: Approval

- [ ] Product Owner (Pablo) approval
- [ ] Technical design review (via `/new-feature` Phase 3.3 plan-review loop)
- [ ] Ready for technical design
