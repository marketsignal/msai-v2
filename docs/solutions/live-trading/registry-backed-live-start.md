# Registry-backed live-start

**Shipped:** 2026-04-20 (PR `feat/live-path-wiring-registry`)
**Supersedes:** the hardcoded `canonical_instrument_id()` + `PHASE_1_PAPER_SYMBOLS` closed-universe gate that previously drove live-start instrument resolution.

## Problem

`POST /api/v1/live/start-portfolio` → `live_supervisor` → trading subprocess used a 5-symbol hardcoded if/elif chain (`canonical_instrument_id()`) and a closed-universe `PHASE_1_PAPER_SYMBOLS` dict. Adding a 6th symbol required editing Python + redeploying the stack. The DB-backed instrument registry (shipped in PR #32 + #35) was **write-only** from live-start's perspective — operators could warm it via `msai instruments refresh`, but live deploys still rejected anything outside the hardcoded chain.

## Fix (architecture)

Three-surface wiring change, enforced by the 2026-04-19 council verdict (`docs/decisions/live-path-registry-wiring.md`):

1. **New pure-read resolver** `lookup_for_live(symbols, as_of_date, session)` at `backend/src/msai/services/nautilus/security_master/live_resolver.py` — registry-only, no IB qualifier, no upserts, no silent fallbacks. Dotted-alias vs. bare-ticker branching mirrors `SecurityMaster.resolve_for_live`. Returns `list[ResolvedInstrument]` with per-row `contract_spec: dict` (IB-SDK compatible) + `canonical_id` + `asset_class` + `effective_window`. Designed so future options support adds `contract_spec` keys without changing the resolver signature.

2. **Typed error hierarchy** that subclasses `ValueError` so the supervisor's `ProcessManager.spawn()` permanent-catch fires correctly (resolver errors otherwise land in the transient-retry branch):
   - `RegistryMissError` — one or more symbols absent; includes copy-pastable `msai instruments refresh --symbols X` hint.
   - `RegistryIncompleteError` — matched row has NULL/malformed required field.
   - `UnsupportedAssetClassError` — option/crypto not wired for live.
   - `AmbiguousRegistryError(reason=...)` — cross-asset-class conflict (wraps registry-layer `AmbiguousSymbolError`) OR same-day-overlap operator seeding.

3. **New `FailureKind` enum variants** (`REGISTRY_MISS`, `REGISTRY_INCOMPLETE`, `UNSUPPORTED_ASSET_CLASS`, `AMBIGUOUS_REGISTRY`). `ProcessManager` dispatches on exception type before `_mark_failed(..., failure_kind=<specific>)`, persisting the resolver's JSON envelope (`to_error_message()`) in `error_message`.

4. **New `EndpointOutcome.registry_permanent_failure(kind, error_message)` factory** returns HTTP 422 + `{"error": {code, message, details}, "failure_kind"}` per project API-design rules. Parses the JSON envelope into structured `details` (e.g., `missing_symbols: [...]`). `cacheable=False` — registry failures are operator-correctable, so retry-after-`msai instruments refresh` must work with the same `Idempotency-Key`.

5. **IB preload builder** `build_ib_instrument_provider_config_from_resolved(resolved)` reconstructs `IBContract` from each `contract_spec` dict, filtering unknown keys for options forward-compat. `build_portfolio_trading_node_config` aggregates `member.resolved_instruments` dedup'd by `canonical_id` across strategy members.

6. **Supervisor payload factory** threads the resolver output: calls `lookup_for_live`, attaches `resolved_instruments=tuple(...)` to each `StrategyMemberPayload`, raises on empty member instruments (programmer bug guard).

## Data contract

Registry row pair → `ResolvedInstrument`:

| Asset class | `contract_spec` dict shape                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------- |
| `equity`    | `{secType: "STK", symbol, exchange=routing_venue, primaryExchange=listing_venue, currency: "USD"}`      |
| `fx`        | `{secType: "CASH", symbol=base, exchange=routing_venue, currency=quote}` (split `raw_symbol` on `/`)    |
| `futures`   | `{secType: "FUT", symbol, exchange=routing_venue, lastTradeDateOrContractMonth, currency: "USD"}`       |
| `option`    | Raises `UnsupportedAssetClassError` at resolver boundary (this PR doesn't wire options — deferred PRD). |
| `crypto`    | Same — raises `UnsupportedAssetClassError`.                                                             |

**Futures month-code parsing** (`_parse_futures_expiry`): decade is inferred from `effective_from.year` with a forward-boundary fix — `2029-12-15 + ESH0.CME → 203003` (not 2020). Tests cover both the in-decade baseline (2026/ESM6 → 202606) and decade-rollover.

## Key design invariants

1. **`as_of_date` is required**, no default. `InstrumentRegistry.find_by_alias` + `require_definition` had UTC defaults that could silently regress roll-day behavior if a future caller forgot to pass Chicago-local `spawn_today`. Task 3b removed the defaults; all callers thread the value explicitly.

2. **Overlap tie-break is explicit + deterministic, not arbitrary.** When multiple active aliases share the max `effective_from`, the resolver raises `AmbiguousRegistryError(reason=SAME_DAY_OVERLAP)` — does NOT silently lexicographic-sort. The PRD's "pick most recent effective_from" rule only applies when the max is unique.

3. **`AmbiguousSymbolError` is wrapped into `AmbiguousRegistryError`**, a `ValueError` subclass, so the supervisor's permanent-catch fires (vs. the transient-retry branch that would catch a bare `Exception`).

4. **No HTTP preflight in `/api/v1/live/start-portfolio`.** Error classification flows through the existing poll-`live_node_processes.failure_kind`-and-map flow. PRD §2 non-goals + council Option C deferral. The API handler only adds dispatch logic (registry kinds → new factory; legacy kinds → existing 503 factory).

5. **`canonical_instrument_id()` leaves the runtime path.** It stays in `live_instrument_bootstrap.py` for the CLI seeding at `msai instruments refresh`. AST regression test (`tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py`) enforces this — walks the AST of `live_supervisor/__main__.py` + `live_node_config.py` and fails on any Name/Attribute/Import reference.

6. **Bounded alerting.** `_fire_alert_bounded` matches `alerting.py:305-328` pattern — `loop.run_in_executor(_HISTORY_EXECUTOR, ...)` + `asyncio.wait_for(shield, timeout=_HISTORY_WRITE_TIMEOUT_S)`. Prevents a wedged alerts volume from hanging the live-start critical path.

## Telemetry

- Log: `event=live_instrument_resolved` with `{source, symbol, canonical_id, asset_class, as_of_date}`. Emitted per-symbol on success (`source=registry`), per-missing-symbol on miss (`source=registry_miss`), per-corrupt-row on incomplete (`source=registry_incomplete`).
- Counter: `msai_live_instrument_resolved_total{source, asset_class}` via the project's hand-rolled `MetricsRegistry` (NOT `prometheus_client` — that dependency is intentionally not in use). Exposed via `/metrics`. Cardinality ≤ 15 time-series.
- Alert: WARN on miss (`alerting_service.send_alert("warning", ...)`), ERROR on incomplete.

## Observability during the drill

From the drill runbook (`docs/runbooks/drill-live-path-registry-wiring.md`):

```
docker compose logs live-supervisor | grep live_instrument_resolved
# → event=live_instrument_resolved source=registry symbol=QQQ ...
```

`source=registry_miss` or `source=registry_incomplete` → ABORT drill, fix registry, restart. The supervisor fails the spawn via the permanent-catch path and the endpoint returns HTTP 422 with a structured body.

## Related docs

- **PRD:** `docs/prds/live-path-wiring-registry.md`
- **Discussion:** `docs/prds/live-path-wiring-registry-discussion.md`
- **Research:** `docs/research/2026-04-20-live-path-wiring-registry.md`
- **Council verdict:** `docs/decisions/live-path-registry-wiring.md`
- **Plan:** `docs/plans/2026-04-20-live-path-wiring-registry.md` (4 plan-review iterations)
- **Drill runbook:** `docs/runbooks/drill-live-path-registry-wiring.md`
- **E2E use cases:** `tests/e2e/use-cases/live/registry-backed-deploy.md`

## Follow-ups (tracked in `CONTINUITY.md`)

- **Symbol Onboarding UI/API/CLI (#3b):** user-facing surfaces to declare "add symbol X of asset class Y" with auto-triggered historical ingest + registry refresh + portfolio-bootstrap. Depends on this shipping; separate PRD + council required.
- **`instrument_cache` → registry migration:** legacy cache table still coexists; scheduled for a follow-up PR.
- **`canonical_instrument_id()` deletion from `live_instrument_bootstrap.py`:** keep for one clean paper week post-merge; remove after operators confirm CLI seeding has no regressions.
- **HTTP preflight (Option C from council):** deferred; revisit once the runtime path has produced a week of clean telemetry.
