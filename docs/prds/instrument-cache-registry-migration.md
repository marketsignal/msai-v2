# PRD: Instrument Cache → Registry Migration

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-27
**Last Updated:** 2026-04-27

---

## 1. Overview

Retire the legacy `instrument_cache` Postgres table and the closed-universe `canonical_instrument_id()` helper, leaving the `instrument_definitions` + `instrument_aliases` registry (PR #32 + #35 + #37 + #44 + #45) as the **single source of truth** for instrument metadata. This is an internal-mechanics migration — there are no new user-facing features. Pablo's binding user stories live in [`docs/prds/symbol-onboarding.md`](symbol-onboarding.md) (PR #45, ratified 2026-04-25); this PR makes the runtime align with that PRD's "registry as sole authority" promise by deleting the parallel data path that quietly contradicts it.

This PRD records the council-ratified scope (5 advisors + Codex xhigh chairman, 2026-04-27) so the implementation plan, code review, and E2E phases all share a single source of binding decisions on Q1–Q10. The mechanics decisions are pinned; future deviation requires its own council pass.

## 2. Goals & Success Metrics

### Goals

- **Primary:** make the registry the only place runtime code reads instrument metadata from. After this PR, `instrument_cache` does not exist; `canonical_instrument_id()` does not exist; `SecurityMaster.resolve_for_live` cold-miss canonicalization does not exist; live cold-miss is operator action via `msai instruments refresh`.
- **Secondary:** preserve the runtime behaviors operators depend on today — backtest resolve, live deploy, market-hours awareness, futures-month qualification — through equivalent registry-backed paths, with no functional regression.
- **Secondary:** establish a structural guard test that fails if any future code reintroduces a runtime reference to the deleted symbols (legacy cache imports or `canonical_instrument_id` references).
- **Secondary:** preserve operator-recoverable migration safety via fail-loud preconditions, `pg_dump` checkpoint, and zero schema/code skew window.

### Success Metrics

| Metric                                                                    | Target                                                 | How Measured                                                                                                                             |
| ------------------------------------------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Runtime references to `instrument_cache` table or `InstrumentCache` model | **0** in `backend/src/`                                | `rg -n "instrument_cache\|InstrumentCache" backend/src` returns 0 hits                                                                   |
| Runtime references to `canonical_instrument_id` symbol                    | **0** in `backend/src/`                                | `rg -n "canonical_instrument_id" backend/src` returns 0 hits                                                                             |
| Backtest + paper-deploy E2E pass after cutover                            | 100%                                                   | `verify-e2e` agent runs the regression suite + the new use cases on the post-migration stack                                             |
| Branch-local restart proof: paper deploy with open positions rehydrates   | passes manually before merge                           | Documented runbook output: `compose down → pg_dump → alembic upgrade head → compose up → spawn paper deploy → confirm Redis rehydration` |
| Active `LiveDeployment.canonical_instruments` registry coverage           | 100% (all 7 active rows pre-validated)                 | Preflight script exits 0 before `alembic upgrade head` is run                                                                            |
| `MarketHoursService.is_in_rth/eth` correctness preserved                  | All existing tests + 1 new test against the new column | Unit + integration test pass on registry-backed `MarketHoursService`                                                                     |
| Schema/code skew window during cutover                                    | 0 seconds                                              | Code rewrite, alembic migration, and worker restart ship as one coordinated commit + restart sequence per runbook                        |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Effective-window child tables for `trading_hours` or `ib_contracts`.** Council overruled the Steward's child-table-with-temporal-discipline proposal in this PR. Trading-hours change so rarely that a JSONB column on `instrument_definitions` is sufficient. A future schema change can introduce windowing if a real multi-row history requirement appears.
- ❌ **Backwards-compatibility shim** keeping `instrument_cache` alive for one release with a synchronous proxy. Hard cutover only — the shim would preserve dual authority.
- ❌ **Retaining `nautilus_instrument_json` or `ib_contract_json`** "for later" / "in case refresh ever uses it." Both columns are deleted, not migrated. Nautilus's `CacheConfig(database=redis)` is the runtime instrument cache; IB qualification re-runs are operator-driven via `msai instruments refresh`.
- ❌ **Splitting the work into two PRs** (cache cutover then `canonical_instrument_id()` removal). Council overruled the split camps — splitting would knowingly ship a release boundary where one of the two parallel data paths still exists.
- ❌ **Skip+log handling of malformed legacy cache rows.** Migration aborts loudly on any orphan that can't be cleanly upserted into the registry. Cleanup is operator action.
- ❌ **New end-user features.** This is internal mechanics. The user stories that drive feature work live in [`docs/prds/symbol-onboarding.md`](symbol-onboarding.md).
- ❌ **Multi-operator / production fleet support.** This is Phase 1 single-operator (Pablo). Maintenance window is minutes-to-hours; no zero-downtime SLA applies.
- ❌ **Live wiring re-architecture.** PR #37 already routed `/api/v1/live/start-portfolio` through the registry via `lookup_for_live()`; this PR removes the now-vestigial cold-miss fallback but does not redesign the warm path.
- ❌ **Migration automation in CI/CD.** Operator runs the runbook by hand; no auto-deploy hook is added.

## 3. User Personas

This is internal mechanics — there is one human persona (Pablo, operator + developer) with two lenses.

### Operator (Pablo, day-of)

- **Role:** runs the migration on the dev / paper-trading stack, owns the maintenance window.
- **Permissions:** full Postgres + Docker control on the single Azure VM.
- **Goals:** run one documented sequence (`compose down → pg_dump → alembic upgrade head → compose up → restart workers → smoke test`) and have the stack come back up with zero functional regression.
- **Failure tolerance:** can take a maintenance window of minutes-to-hours; cannot tolerate a stack that comes back up but silently has stale state, missing aliases, or orphaned reads against a dropped table.

### Developer (Pablo, future months)

- **Role:** future reader of this code (and Claude in future sessions).
- **Goals:** never have to remember "trading_hours used to live on instrument_cache, then moved to instrument_definitions in PR #X" or "the closed-universe canonical_instrument_id() helper used to be a fallback path, kept around for one paper week then deleted in PR #Y."
- **Failure tolerance:** zero. Transitional comments, "Coexistence note," "Phase-1 closed universe," and `# legacy fallback` annotations all become debt the moment this PR lands and MUST be removed in the same commit as the code they document.

## 4. User Stories

These stories are derived from `docs/prds/symbol-onboarding.md`'s binding contract (registry as single source of truth, three readiness states, zero manual SQL) plus the council-ratified mechanics from the discussion log. Each story is testable via the success metrics in §2 or via E2E use cases designed in Phase 3.2b.

---

### US-001: Migrate instrument_cache rows into the registry

**As an** operator
**I want** a single Alembic migration that takes every `instrument_cache` row and upserts the canonical metadata into `instrument_definitions` + `instrument_aliases`, then drops `instrument_cache` and its model
**So that** the registry holds the legacy data without manual SQL and the legacy table cannot be accidentally read or written after the upgrade.

**Scenario:**

```gherkin
Given the dev stack is up with N rows in `instrument_cache`
And the registry is wired since PR #32 + #35 + #37 + #44 + #45
When I run `compose down → pg_dump → alembic upgrade head → compose up`
Then `instrument_cache` is dropped from the schema
And every row's `(canonical_id, asset_class, venue, trading_hours)` has produced a corresponding `instrument_definitions` + `instrument_aliases` pair
And `nautilus_instrument_json` and `ib_contract_json` from the legacy rows are NOT carried forward
And `trading_hours` from the legacy rows IS carried forward to `instrument_definitions.trading_hours`
And no application code references `InstrumentCache` or the table name
And the alembic head moves to the new revision id
```

**Acceptance Criteria:**

- [ ] New Alembic migration upgrades `instrument_cache` → registry then drops the table.
- [ ] Migration is idempotent against the registry's `(raw_symbol, provider, asset_class)` uniqueness via `ON CONFLICT DO NOTHING`.
- [ ] `models/instrument_cache.py` is deleted; `from msai.models import InstrumentCache` raises `ImportError`.
- [ ] `instrument_definitions` schema gains `trading_hours JSONB NULL` column.
- [ ] `instrument_definitions` does NOT gain `ib_contract_json` or `nautilus_instrument_json` columns.
- [ ] Alembic downgrade is schema-only — recreates the empty `instrument_cache` table; data restoration requires `pg_dump` (documented loudly in the migration docstring + runbook).
- [ ] Same-commit zero-skew rollout: code rewrites that drop `_read_cache` / `_upsert_cache` / `_instrument_from_cache_row` ship in the SAME commit as the migration.
- [ ] Worker restart sequence is documented in the runbook per `feedback_restart_workers_after_merges.md`.

**Edge Cases:**

| Condition                                                                      | Expected Behavior                                                                                                                                                   |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Legacy row's `canonical_id` won't parse cleanly (malformed venue suffix, etc.) | **Fail loud** — Alembic raises with operator-readable message naming the row's PK; operator inspects + fixes via `psql` or deletes the row, then re-runs migration. |
| Legacy row already has a registry equivalent (idempotent re-run)               | `ON CONFLICT DO NOTHING` makes the upsert a no-op; migration succeeds.                                                                                              |
| `instrument_cache` is empty                                                    | Migration succeeds; only the schema change applies; worker restart still required.                                                                                  |
| Operator forgets `pg_dump` and rolls back                                      | Downgrade recreates empty table; legacy data is lost. Documented as "operator must `pg_dump` before upgrade" in runbook + migration docstring.                      |

**Priority:** Must Have

---

### US-002: Pre-cutover preflight gates the migration on live-deploy registry coverage

**As an** operator
**I want** a preflight script that fails LOUD before `alembic upgrade head` runs, if any active `LiveDeployment.canonical_instruments` entry doesn't resolve through the registry today
**So that** I can run `msai instruments refresh --symbols X` to seed the missing alias and re-run the preflight, instead of discovering the gap when a paper deploy supervisor crashes on first restart after cutover.

**Scenario:**

```gherkin
Given there are 7 active LiveDeployment rows
And one row has `canonical_instruments=['ESM6.CME', 'AAPL.NASDAQ']`
And the registry has an active alias for 'AAPL.NASDAQ' but NOT for 'ESM6.CME'
When I run `python scripts/preflight_cache_migration.py`
Then the script exits non-zero
And prints: "Active deployment <slug>: 'ESM6.CME' has no registry alias. Run `msai instruments refresh --symbols ES --provider interactive_brokers` to seed, then retry preflight."
And the operator runs the suggested command, the alias is seeded, the preflight re-runs and exits 0
And only then does the operator run `alembic upgrade head`
```

**Acceptance Criteria:**

- [ ] `scripts/preflight_cache_migration.py` exists.
- [ ] Reports legacy `instrument_cache` row count.
- [ ] Iterates every `LiveDeployment` row where `status IN ('starting','running','paused')`.
- [ ] For each row's `canonical_instruments` list, attempts `lookup_for_live(symbols, today)` against the registry.
- [ ] Exits 0 only if every symbol resolves; exits non-zero otherwise with operator-copyable `msai instruments refresh --symbols X` hint.
- [ ] Also reports broader registry invariants the migration will rely on (e.g. no zero-width alias windows, no orphan definition rows).
- [ ] Listed in the runbook as a required step before `alembic upgrade head`.
- [ ] Has its own integration test exercising both the pass-case (all symbols in registry) and the fail-case (one symbol missing → preflight aborts with the expected message).

**Edge Cases:**

| Condition                                                             | Expected Behavior                                                                                                                         |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 0 active deployments                                                  | Preflight exits 0, prints "No active deployments — no canonical_instruments to validate. Proceeding with registry-invariant checks only." |
| Active deployment in `stopped` or `failed` state                      | Skipped — preflight only validates `starting` / `running` / `paused`.                                                                     |
| Symbol resolves through registry today but historical alias is closed | Preflight uses `today` as `as_of` — historical aliases don't matter for restart-time correctness.                                         |
| Registry has the row but ambiguous (cross-asset-class match)          | Preflight raises the same `AmbiguousSymbolError` the runtime would; operator fixes the source data before migrating.                      |

**Priority:** Must Have

---

### US-003: Runtime code stops reading `instrument_cache` everywhere

**As a** developer reading the code 6 months from now
**I want** every runtime read of `instrument_cache` (the `_read_cache` / `_read_cache_bulk` paths in `SecurityMaster`, the `MarketHoursService.load()` query against `InstrumentCache.trading_hours`) replaced with the registry-backed equivalent
**So that** there are no orphan readers pointed at a dropped table after migration, no stale "trading_hours used to live elsewhere" comments cluttering the codebase, and no runtime crashes if the table is dropped.

**Scenario:**

```gherkin
Given the migration has run on the dev stack
When the backend container starts up
And a backtest is submitted via /api/v1/backtests/run
And a paper deploy is started via /api/v1/live/start-portfolio
Then no code path executes a SELECT against the `instrument_cache` table
And `MarketHoursService.is_in_rth(instrument_id)` returns the same answer it did before migration for the same instrument
And `SecurityMaster.resolve()` / `bulk_resolve()` succeeds for any instrument the registry knows about
And no docstring or comment in `backend/src/` references `instrument_cache`, `_read_cache`, `_upsert_cache`, or `nautilus_instrument_json` except in the migration's Alembic file's docstring
```

**Acceptance Criteria:**

- [ ] `SecurityMaster.resolve(spec)` rewritten to use registry alias lookup + Nautilus's Redis-backed cache for instrument hydration; no Postgres `_read_cache` call.
- [ ] `SecurityMaster.bulk_resolve` rewritten similarly with bulk registry query.
- [ ] `_read_cache`, `_read_cache_bulk`, `_write_cache`, `_upsert_cache`, `_instrument_from_cache_row` deleted from `services/nautilus/security_master/service.py`.
- [ ] `MarketHoursService.load()` rewritten to read `instrument_definitions.trading_hours` instead of `instrument_cache.trading_hours`.
- [ ] All transitional comments removed: "Coexistence note", "follow-up PR", "Phase-1 closed universe", "instrument_cache" docstring references in `security_master/specs.py`, `security_master/parser.py`, `risk/risk_aware_strategy.py`, `services/nautilus/instruments.py`, `services/nautilus/trading_node_subprocess.py`.
- [ ] `rg -n "instrument_cache\|InstrumentCache" backend/src` returns ONLY the Alembic migration file (acceptable — it documents the historical migration in its docstring) and 0 production-source matches.
- [ ] Unit test for `MarketHoursService` against the new column passes (no behavior change vs. legacy).

**Edge Cases:**

| Condition                                                                    | Expected Behavior                                                                                                           |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `MarketHoursService` is queried before any definition is loaded              | Same as today — fail-open returning `True` (`market_hours.py:187-189`); preserves current strategy-side semantics.          |
| Registry alias lookup misses for a symbol historically in `instrument_cache` | Migration's pre-cutover step ensured this can't happen; runtime miss is a registry data-integrity bug, not a migration bug. |
| Legacy `instrument_cache` row had `trading_hours = NULL`                     | Migrates as `instrument_definitions.trading_hours = NULL`; `MarketHoursService` fail-opens (current semantics).             |

**Priority:** Must Have

---

### US-004: `canonical_instrument_id()` is fully deleted from runtime + tests

**As a** developer
**I want** both definition sites and every runtime caller of `canonical_instrument_id()` removed in the same PR as the cache cutover
**So that** the registry is the SOLE authority for canonical instrument resolution and no closed-universe oracle survives as a parallel truth source.

**Scenario:**

```gherkin
Given `services/nautilus/instruments.py:90` defines `canonical_instrument_id`
And `services/nautilus/live_instrument_bootstrap.py:146` defines `canonical_instrument_id`
And `services/nautilus/security_master/service.py` calls it in 4 round-trip-validation sites
And `cli.py:799` calls it for futures-month canonicalization in `msai instruments refresh`
When this PR is merged
Then both definitions are deleted (the modules either removed or stripped to non-canonical-only contents)
And the 4 call sites in `security_master.service` are removed
And `SecurityMaster.resolve_for_live` cold-miss canonicalization (line 398 area) is removed entirely — registry miss is fail-loud
And `cli.py:799` uses direct provider/root normalization + IB qualification (NOT a registry lookup — that would be circular per Simplifier's catch)
And the supervisor (`live_supervisor/__main__.py:358, 376`) does NOT import `canonical_instrument_id` (already true post-PR-#37; verify)
And `_ROLL_SENSITIVE_ROOTS` block (`security_master/service.py:130-135 + 374-380`) is deleted as dead code
And `tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` is deleted and replaced with a stronger structural guard
```

**Acceptance Criteria:**

- [ ] `def canonical_instrument_id` does not appear anywhere in `backend/src/`.
- [ ] `from … import canonical_instrument_id` does not appear anywhere in `backend/src/`.
- [ ] `SecurityMaster.resolve_for_live` cold-miss path is removed (Maintainer's binding objection); registry miss raises a typed error with operator-action hint.
- [ ] `cli.py` `instruments refresh --provider interactive_brokers` futures-month handling does direct root-symbol normalization + IB qualification, NOT a registry lookup (Simplifier's binding objection — circular).
- [ ] `_ROLL_SENSITIVE_ROOTS` constant + the `service.py:374-380` block that consumes it are deleted.
- [ ] `tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` is deleted.
- [ ] A NEW structural guard test exists at the same location (or equivalent) that fails if any runtime path imports a forbidden legacy name (`canonical_instrument_id`, `InstrumentCache`, `_read_cache`, `_instrument_from_cache_row`).

**Edge Cases:**

| Condition                                                                            | Expected Behavior                                                                                                                                   |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Operator runs `msai instruments refresh --symbols ES --provider interactive_brokers` | CLI directly normalizes "ES" → IB root + qualifies via IB Gateway; result upserts into registry. Behavior matches today's output for futures-month. |
| Live deploy attempts a registry cold-miss after migration                            | Fails loud with operator-action hint: "Symbol X not in registry. Run `msai instruments refresh --symbols X --provider interactive_brokers`."        |
| A test fixture still imports `canonical_instrument_id`                               | Fixture is migrated to use registry-backed setup; structural guard catches any missed call sites before merge.                                      |

**Priority:** Must Have

---

### US-005: Branch-local restart drill produces evidence of Redis-backed instrument durability

**As an** operator
**I want** to perform a documented paper-trading restart drill on this branch — spawn a paper deploy, leave it with at least one open position, run the migration, restart the stack, confirm the deploy resumes correctly with no `instrument_cache` access
**So that** I have concrete evidence that Nautilus's `CacheConfig(database=redis)` is the runtime instrument cache and the migration doesn't silently rely on a Postgres path that's about to disappear.

**Scenario:**

```gherkin
Given a paper deploy is running on the pre-migration stack
And the deploy has 1+ open position on a paper IB account (DUP*** etc.)
When I run the documented migration sequence
And restart the stack
Then the deploy resumes (or is cleanly stopped — operator's choice via /live/kill-all then restart)
And the supervisor's instrument hydration uses Nautilus's Redis-backed cache, NOT instrument_cache
And `position_reader.py` rehydrates the open position correctly (gotcha #19 reconciliation)
And no log line indicates a SELECT against `instrument_cache`
And the drill output is captured in the PR description as Phase 5.4 evidence
```

**Acceptance Criteria:**

- [ ] Drill runbook exists at `docs/runbooks/instrument-cache-migration.md`.
- [ ] Drill is performed on the branch before merge (operator action, captured in PR description).
- [ ] Drill output explicitly verifies: (a) deploy resumed (or cleanly stopped), (b) Redis rehydration via `position_reader.py` works for the open position, (c) reconciliation completes (gotcha #10), (d) no `instrument_cache` access in container logs.
- [ ] Drill is also exercised as an E2E use case (Phase 3.2b → Phase 5.4 / 5.4b).

**Edge Cases:**

| Condition                                                  | Expected Behavior                                                                                       |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Paper IB Gateway not reachable during the drill            | Drill is paused; operator confirms IB Gateway is up before retrying. Migration is NOT retried half-way. |
| Drill discovers a real bug                                 | "No bugs left behind" applies — fix in the same branch, re-run drill, then merge.                       |
| Drill fails on a non-migration-related issue (flaky infra) | Retry once; if still failing after 2 attempts, escalate to user. Do not paper over with `--no-verify`.  |

**Priority:** Must Have

---

### US-006: Test fixtures + structural guard prevent architectural backsliding

**As a** developer or future Claude session
**I want** a structural test that fails CI if any new code reintroduces a runtime reference to `canonical_instrument_id`, `InstrumentCache`, or other deleted legacy symbols
**So that** the architecture cannot quietly regress in a future PR (the way `/api/v1/universe` survived 4 PRs after the registry was supposed to be canonical).

**Scenario:**

```gherkin
Given the migration has merged
When a future PR adds `from msai.models import InstrumentCache` anywhere in `backend/src/`
Then the structural guard test fails CI
And the failure message names the forbidden symbol + the file that imported it + the canonical replacement (e.g. "use registry alias lookup via SecurityMaster.lookup_for_live")
```

**Acceptance Criteria:**

- [ ] A structural unit test exists (replacing `test_canonical_instrument_id_runtime_isolation.py`) that walks the AST or `rg`'s the production tree for forbidden symbols.
- [ ] Forbidden-symbol list includes at minimum: `canonical_instrument_id`, `InstrumentCache`, `_read_cache`, `_upsert_cache`, `_instrument_from_cache_row`, `_ROLL_SENSITIVE_ROOTS`.
- [ ] Test is fast (< 1s); runs as part of `pytest tests/unit/`.
- [ ] Test does NOT misclassify the migration's own Alembic file (which legitimately mentions `instrument_cache` in its docstring). Either scope to `backend/src/msai/` or include explicit allowlist.
- [ ] The 5 cache-touching test files are migrated to registry semantics (not just deleted) where the test value is real:
  - `tests/integration/test_instrument_cache_model.py` → DELETE (model is gone)
  - `tests/integration/test_security_master.py` → MIGRATE to registry semantics
  - `tests/integration/test_security_master_resolve_live.py` → MIGRATE
  - `tests/integration/test_security_master_resolve_backtest.py` → MIGRATE
  - `tests/integration/test_instrument_definition_crud.py` → KEEP (already registry-only)
  - `tests/e2e/test_security_master_phase2.py` → MIGRATE to registry semantics
- [ ] One new market-hours test against the new `instrument_definitions.trading_hours` column.
- [ ] One new migration/backfill test exercising the upsert + drop path with a representative legacy-row fixture.
- [ ] One new CLI test exercising direct provider/root normalization + qualification (replacing the `canonical_instrument_id`-based path).

**Priority:** Must Have

---

## 5. Constraints & Policies

> Outcome-level constraints. The implementation plan elaborates HOW. Council-ratified mechanics decisions are recorded in §9 to ensure the plan honors them.

### Business / Compliance Constraints

- **No data loss for trading-hours metadata.** Any RTH/ETH window data currently in `instrument_cache.trading_hours` must be queryable post-migration via the registry. Policy: hard requirement.
- **No silent state drift.** The migration must surface any malformed legacy row rather than skip + log; the operator must explicitly resolve the corruption before retrying.
- **No new compliance surface.** This is internal infra — no PII, no payment, no auth surface, no regulatory reporting affected.

### Platform / Operational Constraints

- **Dev compose stack only.** This PR ships to Pablo's single-VM dev stack (Docker Compose). No multi-VM rollout, no canary, no zero-downtime SLA.
- **Maintenance window: minutes-to-hours.** Operator can take the stack down (`compose down → migrate → compose up`) without coordination.
- **Postgres 16 + Redis 7.** No version migration is part of this PR.
- **Schema/code skew window: 0 seconds.** Code rewrite + alembic migration + worker restart ship as one coordinated commit + restart sequence per the runbook.

### Dependencies & Required Integrations

- **Requires:** `instrument_definitions` + `instrument_aliases` registry (PR #32 + #35 + #37 + #44 + #45) — present.
- **Requires:** Nautilus `CacheConfig(database=redis)` wired in `live_node_config.py:309, 546` + `projection/position_reader.py:140` — present.
- **Requires:** `lookup_for_live()` over the registry (PR #37) — present.
- **Requires:** `test_cache_redis_instrument_roundtrip.py` proving Cache→Redis→Cache round-trip durability — present.
- **Blocked by:** none.
- **Named integrations (scope):** Nautilus's own `CacheConfig(database=redis)` is the runtime instrument cache (Nautilus API, not MSAI implementation). IB Gateway's `InteractiveBrokersInstrumentProvider` is the qualification path for `msai instruments refresh --provider interactive_brokers`. No new external integrations.

## 6. Security Outcomes Required

This PR does not introduce new security surface. Existing outcomes are preserved:

- **Who can access what:** unchanged. Migration runs as the dev-stack Postgres user; production has no separate role boundaries beyond what already exists.
- **What must never leak:** unchanged. No secrets, tokens, or PII pass through this code path.
- **What must be auditable:** unchanged. Alembic's standard migration log is the audit trail.
- **What legal/regulatory outcomes apply:** none beyond existing.

## 7. Open Questions

These are the council's "Missing Evidence" items — to be resolved during research / planning / TDD execution. They do NOT block PRD ratification but MUST be resolved before merge.

- [ ] **OQ-001 — Branch-local restart proof.** Per US-005, perform a paper-deploy-with-open-positions restart drill on this branch and capture the output. Resolves the Live-Trading Safety Officer's binding evidence requirement.
- [ ] **OQ-002 — Active deployment preflight result.** Run the preflight script against the 7 active `LiveDeployment` rows. Determine whether every listed symbol resolves through the registry today, OR identify which need `msai instruments refresh` first. Resolves the Live-Trading Safety Officer's pre-cutover gate requirement.
- [ ] **OQ-003 — Orphan profile in `instrument_cache`.** Run `psql` to inspect existing rows; determine whether any have malformed `canonical_id`, missing venue suffix, or other corruption. If yes, decide: fix at source first, or accept the migration aborting and retry after manual cleanup. Resolves the Maintainer's "fail-loud orphan handling" reality check.
- [ ] **OQ-004 — CLI futures-month coverage.** Verify that the new `cli.py` direct-provider/root-normalization + IB-qualification path for `msai instruments refresh` covers the futures-month cases formerly handled by `canonical_instrument_id` (e.g. `ES → ESM6.CME` near June 2026, holiday-adjusted Juneteenth shift, etc.). Resolves the Simplifier's circular-CLI catch.
- [ ] **OQ-005 — Structural-guard scope.** Decide whether the new structural guard test (US-006) walks the AST (faster, more precise) or `rg`'s the source tree (simpler, may have false positives). Whichever is chosen, document the allowlist semantics so the migration's Alembic file isn't itself flagged.

## 8. References

- **Discussion log:** [`docs/prds/instrument-cache-registry-migration-discussion.md`](instrument-cache-registry-migration-discussion.md)
- **Authoritative user contract (the PRD this branch serves):** [`docs/prds/symbol-onboarding.md`](symbol-onboarding.md)
- **Skeleton plan (8 tasks, parent PR's split-off):** [`docs/plans/2026-04-17-db-backed-strategy-registry.md`](../plans/2026-04-17-db-backed-strategy-registry.md) §"Split-off PR Skeleton — InstrumentCache → Registry Migration" (lines 2694–2715)
- **Council verdict (binding mechanics decisions):** persisted at the end of the discussion log.
- **PR #32 (registry tables):** [`docs/prds/db-backed-strategy-registry.md`](db-backed-strategy-registry.md)
- **PR #37 (live-path wiring):** [`docs/decisions/live-path-registry-wiring.md`](../decisions/live-path-registry-wiring.md)
- **PR #44 (Databento bootstrap):** [`docs/prds/databento-registry-bootstrap.md`](databento-registry-bootstrap.md)
- **PR #45 (Symbol Onboarding):** [`docs/prds/symbol-onboarding.md`](symbol-onboarding.md)
- **NautilusTrader gotchas (#7, #9, #10, #11, #16, #19):** [`.claude/rules/nautilus.md`](../../.claude/rules/nautilus.md)

## 9. Council-Ratified Mechanics Decisions (Binding)

These decisions answer the 10 mechanics questions raised in the discussion log; they are **binding for this PR** and the implementation plan must respect them. Future deviation requires its own council pass.

| Q   | Question                                                                    | Binding decision                                                                                                                                                                                                                                                                                                                                                                   |
| --- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | Combined or split PR?                                                       | **(a) Combined** — cache cutover + full `canonical_instrument_id()` removal in one PR.                                                                                                                                                                                                                                                                                             |
| Q2  | Migration-time validation of active `LiveDeployment.canonical_instruments`? | **Yes** — preflight script aborts on any miss; validates both deployment-backed symbols + registry invariants.                                                                                                                                                                                                                                                                     |
| Q3  | `trading_hours` target?                                                     | **(a) JSONB column on `instrument_definitions`** — child-table redesign deferred to a future PR if needed.                                                                                                                                                                                                                                                                         |
| Q4  | Delete `nautilus_instrument_json` entirely?                                 | **Yes — unanimous.**                                                                                                                                                                                                                                                                                                                                                               |
| Q5  | `ib_contract_json` target?                                                  | **(b) Drop entirely** — no post-migration owner/reader exists.                                                                                                                                                                                                                                                                                                                     |
| Q6  | Orphaned legacy rows?                                                       | **(a) Fail-loud abort** — no silent skip+log; operator resolves before retrying.                                                                                                                                                                                                                                                                                                   |
| Q7  | Hard cutover or shim?                                                       | **(a) Hard cutover, no shim — unanimous.**                                                                                                                                                                                                                                                                                                                                         |
| Q8  | Skeleton precondition cleared?                                              | **Yes** — but evidence bar raised: branch-local restart proof required (US-005).                                                                                                                                                                                                                                                                                                   |
| Q9  | `canonical_instrument_id()` removal scope?                                  | **Full removal expanded** — both definitions deleted; 4 round-trip-validation sites in `security_master.service` removed; `SecurityMaster.resolve_for_live` cold-miss canonicalization removed; CLI `cli.py:799` uses direct provider/root normalization + IB qualification (NOT registry lookup — Simplifier's circular-CLI catch); `_ROLL_SENSITIVE_ROOTS` deleted as dead code. |
| Q10 | Test fixture strategy?                                                      | **Stronger replacement** — migrate behavior tests to registry semantics; delete cache-only + helper-isolation tests; ADD a structural guard that fails on any runtime import/reference of legacy cache/helper code.                                                                                                                                                                |

**Minority Report (preserved for plan-review reference):**

- **Steward** wanted child tables for `trading_hours` + `ib_contracts` with effective-window discipline; overruled in favor of the simpler JSONB column; deferred to its own future schema change if a real multi-row history requirement appears.
- **Live-Trading Safety Officer** wanted a split (cache first, canonical-helper after one paper-week soak); sequencing overruled (preserves half-migrated architecture); safety mechanisms (preflight, restart drill, dead-code removal) adopted in full.
- **Migration Pragmatist** wanted a split + retain `ib_contract_json`; overruled on both; operational discipline (preflight, `pg_dump`, zero-skew rollout) adopted in full.
- **Simplifier/Contrarian** wanted helper-first split + skip+log orphans; overruled on both; circular-CLI catch adopted in full as a binding constraint on the CLI rewrite.
- **Maintainer** wanted combined cutover + remove live cold-miss canonicalization + stronger structural guard test; mostly adopted; only partial deferral is the validation framing (deployment-row preflight is required IN ADDITION TO registry invariants).

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                                     |
| ------- | ---------- | -------------- | ------------------------------------------------------------------------------------------- |
| 1.0     | 2026-04-27 | Claude + Pablo | Initial PRD. Council verdict (5 advisors + Codex xhigh chairman) ratified mechanics Q1–Q10. |

## Appendix B: Approval

- [ ] Pablo (operator + product owner) approval
- [ ] Council verdict ratified — see §9 + discussion log
- [ ] Ready for technical design (Phase 2: research-first agent → Phase 3: brainstorming + plan)
