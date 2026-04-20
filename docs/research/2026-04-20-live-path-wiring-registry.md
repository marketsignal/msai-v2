# Research: Live-path wiring onto instrument registry

**Date:** 2026-04-20
**Feature:** Wire `/api/v1/live/start-portfolio` → supervisor → subprocess onto the existing DB-backed instrument registry via a new pure-read `lookup_for_live`.
**Researcher:** research-first agent

## Verdict

**N/A-minimal** — no new libraries enter; every touched seam already exists in-tree. Two small adjustments flagged (see Risks). Nautilus field shapes, alerting level API, observability module, and `spawn_today` plumbing all match PRD §8 assumptions.

## Findings (per research target)

### 1. `InteractiveBrokersInstrumentProviderConfig` (Nautilus `>=1.222.0`)

Pin confirmed: `/Users/pablomarin/Code/msai-v2/.worktrees/live-path-wiring-registry/backend/pyproject.toml:27` (`nautilus_trader[ib]>=1.222.0`). Shape is documented in-tree at `/Users/pablomarin/Code/msai-v2/.worktrees/live-path-wiring-registry/docs/nautilus-reference.md:395-432`: two frozenset fields, `load_ids: FrozenSet[InstrumentId]` and `load_contracts: FrozenSet[IBContract]`. Current builder already uses `load_contracts=frozenset(IBContract objects)` with `SymbologyMethod.IB_SIMPLIFIED` + `cache_validity_days=1` (`backend/src/msai/services/nautilus/live_instrument_bootstrap.py:311-315`). PRD's "contract_spec: dict" payload is implementation-internal to the resolver — it must materialise back into `IBContract` before handoff. **No library-driven change.**

### 2. IB adapter preload path

Preload behavior documented at `docs/nautilus-reference.md:434-462`: `load_all_async()` runs at startup; missing instruments log a **WARNING and continue** — startup does NOT fail. Strategy subscription errors only surface at runtime. This is identical to Nautilus gotcha #9 (`.claude/rules/nautilus.md`). Plan must validate the loaded set after `load_all_async()`.

### 3. `TradingNodePayload.spawn_today_iso` plumbing (verified in worktree)

Already end-to-end. Evidence: `backend/src/msai/live_supervisor/__main__.py:126` computes `spawn_today = exchange_local_today()`; line 283 passes `today=spawn_today` to `canonical_instrument_id`; line 353 serializes to `spawn_today_iso`. Subprocess deserializes at `backend/src/msai/services/nautilus/trading_node_subprocess.py:1562-1583` and forwards to `build_trading_node_config(..., spawn_today=spawn_today)`. `live_node_config.py:176,259,424,480` thread it into `build_ib_instrument_provider_config(..., today=spawn_today)`. **New resolver slots in at the same three sites — plumbing is ready.**

### 4. `InstrumentRegistry.find_by_alias` timezone

Council flag confirmed. `backend/src/msai/services/nautilus/security_master/registry.py:47-78`: signature is `find_by_alias(alias_string, *, provider, as_of_date: date | None = None)` with default `as_of_date or datetime.now(UTC).date()` (line 60). Window is `effective_from <= as_of AND (effective_to IS NULL OR effective_to > as_of)`. **Risk:** any caller that omits `as_of_date` inherits UTC — must pass Chicago-local `spawn_today` explicitly. `require_definition` (line 119) has the same default.

### 5. `msai.services.observability.trading_metrics`

**Exists.** `backend/src/msai/services/observability/trading_metrics.py` — 56 lines, uses `get_registry().counter(...)`/`.gauge(...)` pattern. Lazy-registered at import. Adding `msai_live_instrument_resolved_total` with `{source, asset_class}` labels follows the same one-liner pattern (see lines 21-23). PRD §8 Q1 resolved: **use the existing module, don't create new.**

### 6. `alerting_service` WARN level

**Supported.** `AlertService.send_alert(..., level: str = "warning")` at `backend/src/msai/services/alerting.py:296`. Level is mapped via `_LOG_METHOD_BY_ALERT_LEVEL` (line 58) and forwarded to `alerting_service.send_alert(level, subject, body)` (line 314). PRD's `.warn()` phrasing is shorthand — the actual API is `send_alert(..., level="warning")`. PRD §8 Q3 resolved: **no extension needed; use `level="warning"` vs `level="error"`.**

## Plan-review spot checks (PRD §8)

- Q1 `trading_metrics.py` exists: **YES.**
- Q2 `asset_class` enum values: **defer to plan-review** — check `backend/src/msai/models/instrument_definition.py` for the column's enum.
- Q3 `alerting_service` WARN support: **YES** (`level="warning"`).
- Q4 FX ingested: **defer to plan-review** (check `data-status` output).
- Q5 `lookup_for_live` async vs sync: **async** — registry is `AsyncSession`-based (`registry.py:45-78`); all existing callers are async.
- Q6 where `spawn_today` enters provider config builder: `trading_node_subprocess.py:1562-1583` → `live_node_config.py:176,259,424,480` → `live_instrument_bootstrap.build_ib_instrument_provider_config(..., today=)`.

## Risks surfaced

1. **UTC default in `find_by_alias`** — resolver must require `as_of_date` as a positional/required kwarg to prevent silent UTC regressions on roll days (council constraint #3).
2. **IB adapter silently warns on missing preloads** — resolver miss must raise before `load_all_async()`; post-load validation is defense-in-depth.
3. **`load_contracts` is frozenset of `IBContract`** — PRD's `contract_spec: dict` must reconstruct `IBContract(...)`; specify reconstruction in the plan (required fields per asset class).
