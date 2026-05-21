# Fix Plan: catalog/strategy bar_type mismatch — block operator PATCH trap

> **/fix-bug catalog-strategy-bartype-mismatch** — KISS path, revised from Codex's full prescription after Pablo flagged over-engineering 2026-05-20 night.

## Goal

Prevent operators from setting `instrument_id` or `bar_type` via `PATCH /api/v1/strategies/{id}` `default_config`. These are dispatch-time fields injected by the orchestrator; operator-PATCH leaks into candidate snapshots and silently breaks subsequent backtests.

## Architecture

The trap that broke tonight: operator PATCHed `strategy.default_config` to add `bar_type: 1-DAY-...`. The strategy_ids bridge snapshotted that into every `GraduationCandidate.config` created during the buggy window. Even after the strategy default_config was reverted, candidate.config still wins the merge at `orchestration.py:1062`. Result: 0-event backtests until someone manually edited the candidate via psql.

The fix shipped here: at the API boundary, reject PATCH bodies whose `default_config` contains `instrument_id` or `bar_type`. Operators get a clear 422 explaining these are dispatch-time fields, not strategy-level defaults. New candidates can no longer be poisoned. Existing poisoned candidates (one in tonight's session) have been recovered via psql.

## Tech Stack

- FastAPI + Pydantic (validation)
- pytest (TDD)

## What's NOT in scope (deferred / dropped from Codex's prescription)

- **canonical_bar_type helper** — premature abstraction per CLAUDE.md `principles.md` KISS. Three callers of a single string don't warrant a helper. Revisit when multi-resolution support lands.
- **Fail-loud invariant at dispatch** — belt-and-suspenders against future code drift. Add when a second class of bar_type bug appears.
- **`refresh-from-strategy` endpoint** — YAGNI given this fix. After this fix lands, no new candidates can be poisoned. Tonight's one poisoned candidate was already psql-recovered. Document the psql recovery in solution doc for the (now-rare) historical case.
- **Multi-resolution bar_type support** — Codex confirmed: separate `/new-feature` scope.

## The Fix

**File:** `backend/src/msai/api/strategies.py`

Add validator on the PATCH endpoint. Reject 422 with:

```json
{
  "error": {
    "code": "MANAGED_CONFIG_KEY",
    "message": "Cannot set 'instrument_id' / 'bar_type' on strategy default_config. These are dispatch-time fields injected by the portfolio orchestrator from the candidate's `instruments` list + the platform's canonical bar_type. Setting them on default_config silently snapshots into every candidate created during this window and breaks subsequent backtests when reverted.",
    "details": [{"field": "default_config", "rejected_keys": ["<the rejected keys>"]}]
  }
}
```

Blocklist: `{"instrument_id", "bar_type"}`.

## E2E Use Cases

- **UC-CSBM-API-001** (Operator's PATCH attempt to add managed keys is rejected):
  - **Intent:** Operator PATCHes a strategy with `default_config = {instrument_id: "AAPL.NASDAQ"}` thinking they're filling in a default. The API rejects with a clear error explaining the trap.
  - **Interface:** API
  - **Setup:** Strategy exists with no managed keys in default_config.
  - **Steps:** `PATCH /api/v1/strategies/{id}` with `{default_config: {instrument_id: "AAPL.NASDAQ"}}`.
  - **Verification:** Response is 422 with `error.code === "MANAGED_CONFIG_KEY"` and `instrument_id` in `details[0].rejected_keys`.
  - **Persistence:** `GET /api/v1/strategies/{id}` — `default_config` unchanged.

- **UC-CSBM-API-002** (Operator's legitimate PATCH still works):
  - **Intent:** Operator legitimately bumps `fast_ema_period` on a strategy's default_config.
  - **Interface:** API
  - **Setup:** Strategy exists.
  - **Steps:** `PATCH /api/v1/strategies/{id}` with `{default_config: {fast_ema_period: 20}}`.
  - **Verification:** 200 OK; response body's `default_config.fast_ema_period === 20`.
  - **Persistence:** `GET /api/v1/strategies/{id}` returns the updated value.

**Surface coverage decision:**

- API: Covered.
- UI: N/A — no UI surface exists for strategy default_config editing.
- CLI: N/A — no CLI surface for strategy default_config editing today.
