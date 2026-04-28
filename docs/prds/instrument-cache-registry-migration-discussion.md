# PRD Discussion: Instrument Cache → Registry Migration

**Status:** In Progress
**Started:** 2026-04-27
**Participants:** Pablo, Claude

## Pablo's framing (2026-04-27)

> "This is a task, or this is a branch that is more about the mechanics and how things work than my wish as a user. My wish as a user is in the PRD of the symbol onboarding. If we need to fix something so it works that way, if we need to undo things that we did in the past and delete that and clear that, so be it. I need counsel to answer these questions for me because it's too deep in the weeds for me to answer them. I just need to make the best call, knowing that what I want is what I stated on the PRD as a user story in the prior commit."

**Authoritative user stories live in [`docs/prds/symbol-onboarding.md`](symbol-onboarding.md)** (ratified PR #45, 2026-04-25). This branch's purpose is to make the runtime align with that PRD's contract — by migrating `instrument_cache` into the registry that Symbol Onboarding already writes to, and by deleting legacy closed-universe paths (`canonical_instrument_id()`) that contradict the registry being the single source of truth.

**Scope license:** Pablo explicitly authorized "if we need to undo things that we did in the past and delete that and clear that, so be it." Default to the cleanest end-state, not the most conservative migration.

### Implicit operator/developer stories for THIS branch (derived from the symbol-onboarding contract)

- **US-IMP-A**: A symbol onboarded via `/api/v1/symbols/onboard` SHALL produce exactly one canonical-metadata row in `instrument_definitions` + `instrument_aliases`, with NO sibling row in a legacy `instrument_cache` table.
- **US-IMP-B**: Existing `instrument_cache` rows from prior live deploys MUST be migrated into the registry by `alembic upgrade head` — no manual SQL, no operator intervention beyond `compose down → alembic upgrade head → compose up`.
- **US-IMP-C**: After the migration ships, `instrument_cache` table is dropped, `InstrumentCache` model is deleted, importing it raises `ImportError`.
- **US-IMP-D**: After the migration ships, both `canonical_instrument_id()` definitions (closed-universe) are deleted; the registry is the single source of truth for canonical instrument IDs.
- **US-IMP-E**: Trading-hours data (RTH/ETH windows) currently held in `instrument_cache.trading_hours` is preserved and remains queryable by `services/nautilus/market_hours.py` from a registry-side location.
- **US-IMP-F**: Any backtest or live deploy that worked the day before the migration continues to work the day after — same canonical IDs, same instrument shapes, same trading-hours behavior.

## Code reconnaissance (current state)

### `instrument_cache` callers (8 production files + 5 test files)

| Surface                                          | Reads                      | Writes          | Notes                                                                       |
| ------------------------------------------------ | -------------------------- | --------------- | --------------------------------------------------------------------------- |
| `models/instrument_cache.py`                     | —                          | —               | Model definition, Alembic migration `f4a5b6c7d8e9`.                         |
| `services/nautilus/security_master/service.py`   | `_read_cache`/`_bulk`      | `_upsert_cache` | Cache-first read in `resolve_for_backtest`/`_for_live`. **Hot path.**       |
| `services/nautilus/security_master/specs.py`     | docstring refs only        | —               | `InstrumentSpec.canonical_id()` shape contract.                             |
| `services/nautilus/security_master/parser.py`    | docstring refs only        | —               | Trading-hours JSONB schema doc.                                             |
| `services/nautilus/market_hours.py`              | `trading_hours` JSONB      | —               | `MarketHoursService.load(canonical_ids)` — bulk read of the JSONB column.   |
| `services/nautilus/trading_node_subprocess.py`   | `nautilus_instrument_json` | —               | Live-subprocess instrument hydration — **could move to Nautilus cache DB**. |
| `services/nautilus/risk/risk_aware_strategy.py`  | docstring refs only        | —               | Comment about Phase 4 task 4.3 wiring market hours.                         |
| `services/nautilus/live_instrument_bootstrap.py` | —                          | —               | Imports `canonical_instrument_id` (legacy closed-universe).                 |

### `canonical_instrument_id` callers (2 definitions, 5 production callers)

| Definition site                                                                        | Caller                                                                                            |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `services/nautilus/instruments.py:90` (closed-universe equity-default)                 | `services/nautilus/security_master/service.py` (4 sites — fallback + canonical-round-trip checks) |
| `services/nautilus/live_instrument_bootstrap.py:146` (closed-universe live-symbol map) | `cli.py:799` (instruments refresh helper)                                                         |

After PR #37 the live deploy path (`/api/v1/live/start-portfolio`) uses `lookup_for_live(...)` over the registry, NOT `canonical_instrument_id()`. The function is now used only in (a) the `security_master.service` round-trip-validation paths and (b) the `cli.py instruments refresh` futures-month canonicalization helper.

### Skeleton plan vs. current backlog

The skeleton (`docs/plans/2026-04-17-db-backed-strategy-registry.md` §"Split-off PR Skeleton — InstrumentCache → Registry Migration", lines 2694-2715) lists **8 tasks**, but it **does NOT** include `canonical_instrument_id()` removal — that was a separate "Next #5" line item. Pablo's post-PR-45 backlog combined them ("Next #1: cache migration + delete `canonical_instrument_id()`").

### Skeleton precondition

> "this follow-up PR MUST run after `CacheConfig(database=redis)` is verified in production through at least one restart cycle."

`CacheConfig(database=redis)` is wired in `live_node_config.py:309,546` and `projection/position_reader.py:140`. The "verified through restart" claim is implicit in 6 months of live operation post-PR-#28-ish — but the skeleton's gate is qualitative, so we should confirm explicitly.

## Discussion Log

### Round 1 — Refinement Questions (Claude → Pablo)

**Q1 — Scope: combined or split?**
The skeleton plan and "Next #5" treat `canonical_instrument_id()` removal as separable from the cache migration. Three options:

- **(a) Combined PR**: cache migration + delete both `canonical_instrument_id()` definitions in one branch. Higher blast radius, one review surface, one drill.
- **(b) Cache first, canonical removal second**: cache migration ships standalone; a separate trivial follow-up PR deletes `canonical_instrument_id()` once the registry is the only path.
- **(c) Canonical removal first, cache migration second**: delete `canonical_instrument_id()` first (small PR, just rewires `security_master` and `cli`); then bigger cache-migration PR ships independently.

The original CONTINUITY phrasing said "(a) Combined". Confirming because the skeleton skipped it.

**Q2 — Live-deployment safety during migration.**
Live deployments persist `canonical_instruments: list[str]` on `LiveDeployment` (PR #37). When the migration runs:

- **Active deployments at `alembic upgrade head` time** — does the migration need to validate that all in-flight `LiveDeployment.canonical_instruments` entries map cleanly to a registry row? If not, the supervisor's first read after restart could 422 on a stale alias.
- **Mid-rebalance** — `LivePortfolioRevision.snapshot()` holds a `SELECT FOR UPDATE`. Migration runs offline (compose down → alembic up → compose up), so no concurrent snapshot. Confirming that's the operator's expectation.

**Q3 — Trading-hours migration target.**
`market_hours.py` reads `instrument_cache.trading_hours` JSONB. Two options for where this lands:

- **(a) Add `trading_hours JSONB` column to `instrument_definitions`** — co-locates metadata with the canonical row. Simple. Schema gets one more nullable column.
- **(b) New `instrument_trading_hours` child table** — keeps `instrument_definitions` lean; allows multiple historical windows (rarely useful — trading hours change ≤ once per decade).

Skeleton suggests (a) by default. Confirming, OR is there a reason to go (b)?

**Q4 — `nautilus_instrument_json` migration target.**
`instrument_cache.nautilus_instrument_json` (the Nautilus-serialized `Instrument` object) is read in `services/nautilus/security_master/service.py:_instrument_from_cache_row` to rebuild the Nautilus `Instrument` for the live subprocess. The skeleton says:

> "(a) Nautilus payload JSONB (subsumed by `CacheConfig(database=redis)`)"

Confirming the design intent: **delete this column entirely; live subprocess hydrates Instruments via Nautilus's own Redis-backed cache (`CacheConfig.database`) on startup, NOT from a Postgres column.** That's the cleaner path. Backtest path resolves via `TestInstrumentProvider` synchronously (no cache needed). If yes, skeleton task #5 ("Rewrite `trading_node_subprocess.py` instrument loading to use Nautilus cache DB hydration") becomes the load-bearing task.

**Q5 — `ib_contract_json` migration target.**
`instrument_cache.ib_contract_json` (full IB `Contract` fields) is currently used by `SecurityMaster` to rebuild the IB contract without re-hitting IB. Three options:

- **(a) Move to `instrument_definitions.ib_contract_json` column** — preserves the rate-limit-avoidance pattern. Co-located with the canonical row.
- **(b) Drop entirely** — caller re-qualifies via `IBQualifier` on demand. Loses the cached IB shape; cold-start per-symbol IB round trip on every refresh.
- **(c) Move to a new `instrument_ib_contracts` child table** — keyed by registry UUID + provider.

Skeleton implies (a) by inclusion in the migration step. Confirming.

**Q6 — Backfill semantics for orphaned `instrument_cache` rows.**
The migration upserts an `instrument_definition` per `instrument_cache` row (skeleton task #2). What happens to a cache row whose `canonical_id` parsing fails (malformed venue, deprecated symbol, etc.)?

- **(a) Fail-loud** — Alembic raises, operator must clean Postgres before retrying.
- **(b) Skip + log + summary report** — migration logs orphans to `data/migrations/instrument_cache_orphans.json` and continues.
- **(c) Best-effort + dry-run mode** — offer `--dry-run` Alembic mode that reports what would migrate, then a real run after operator review.

(b) is closest to PR #44's bootstrap pattern (HTTP 207 partial-success). (a) is operator-friendlier for one-off migration.

**Q7 — Operator-facing rollback plan if migration fails mid-flight.**
Alembic migrations can be rolled back via `alembic downgrade -1`. Does the migration need:

- **(a) Backwards-compat shim** — keep `instrument_cache` table for one release cycle, add a synchronous proxy `_read_cache(canonical_id)` that reads from the new tables. Allows immediate rollback.
- **(b) No shim, hard cutover** — drop the table, accept that downgrade requires a `pg_dump` restore.

Skeleton says drop in task #7. Confirming hard cutover is acceptable, given the operator can `pg_dump` before running the migration.

**Q8 — Deferred follow-up: live-wiring vs cache migration ordering.**
Skeleton lists THREE follow-up PRs (cache migration, live-wiring, strategy-config-schema). Strategy-config-schema shipped (PR #38). Live-wiring shipped (PR #37). Skeleton's ordering said cache migration "MUST run after CacheConfig(database=redis) is verified in production." That's done. **Confirming there's no remaining ordering gate I'm missing.**

**Q9 — `canonical_instrument_id()` removal: is anything still load-bearing?**
After PR #37, `canonical_instrument_id()` is called from:

- `cli.py:799` — `msai instruments refresh` futures-month canonicalization (e.g. `ES` → `ESM6.CME`). This is operator-tooling, not runtime trading.
- `security_master/service.py` (4 sites) — round-trip validation in `resolve_for_backtest` / `_for_live` (the function's output is compared against the registry's stored alias to detect drift).

If the registry is now the single source of truth, the round-trip checks in `security_master` become redundant (the registry IS what we're checking against). The CLI futures-month helper needs a replacement — likely a new helper that uses the registry instead.

Confirming: the **scope of "delete `canonical_instrument_id()`"** includes (a) deleting the round-trip-check code paths in `security_master.service` and (b) replacing the CLI helper with a registry-backed one.

**Q10 — Test fixture convention + test coverage delta.**
The skeleton notes a fixture convention: "claude-version integration tests use a per-file `session_factory`/`session` pattern from `PostgresContainer`, NOT a generic `db_session: AsyncSession`."

5 test files reference `instrument_cache` directly: `tests/integration/test_instrument_cache_model.py`, `tests/integration/test_security_master.py`, `tests/e2e/test_security_master_phase2.py`, `tests/integration/test_instrument_definition_crud.py`, plus the unit test `test_canonical_instrument_id_runtime_isolation.py`.

Confirming: every cache-touching test gets migrated to the registry; the cache-only tests get deleted; the structural unit test for `canonical_instrument_id` (`test_canonical_instrument_id_runtime_isolation.py`) gets deleted as part of the function's removal.

---

### Round 1 Resolution — Council Verdict (2026-04-27)

Pablo's framing made clear: "I need counsel to answer these questions for me because it's too deep in the weeds for me to answer them. I just need to make the best call, knowing that what I want is what I stated on the PRD as a user story in the prior commit." Q1–Q10 were routed to `/council` (5 advisors + Codex xhigh chairman) on 2026-04-27.

**Personas:** Symbol-Onboarding Contract Steward (Claude), Live-Trading Safety Officer (Claude), Migration Pragmatist (Claude), Simplifier/Contrarian (Codex), Maintainer (Codex). All 5 returned **CONDITIONAL** verdicts.

**Chairman synthesis (Codex xhigh, verbatim):**

#### Recommendation

1. **Q1 — (a) one combined PR.** Sides with Steward + Maintainer over split camps. Pablo's clean-end-state preference + the PRD's sole-authority rule outweigh the diagnostic convenience of sequencing. Mitigate blast radius via strict preflight, same-commit code+migration cutover, and documented restart drill — NOT a half-migrated architecture.
2. **Q2 — yes.** Migration must fail before cutover if any active `LiveDeployment` symbol cannot resolve through the registry. Validate both deployment-backed symbols AND broader registry invariants (Maintainer's caveat).
3. **Q3 — (a) `trading_hours` JSONB on `instrument_definitions`.** 4-advisor majority. Keep migration focused on storage relocation, NOT temporal-model redesign. Steward's effective-window argument deferred to its own future schema change if needed.
4. **Q4 — yes, delete `nautilus_instrument_json`.** Unanimous. Redis/Nautilus is the runtime persistence layer; no verified live path needs the Postgres copy.
5. **Q5 — (b) drop `ib_contract_json` entirely.** `SecurityMaster.refresh()` is not implemented; live resolution no longer depends on this blob; carrying opaque provider payloads "for later" is the dead parallel state this migration removes.
6. **Q6 — (a) fail loud on orphaned/malformed rows.** Active-deployment validation is necessary but not sufficient; silent skips leave unexplained holes. Small row count + hard-cleanup mandate make stop-on-corruption the safer choice.
7. **Q7 — (a) hard cutover, no shim.** Unanimous. Compatibility layer would preserve dual authority. Caution = preflight + restart verification, NOT a temporary bridge.
8. **Q8 — yes.** Architectural precondition cleared, but evidence bar for implementation completion is raised: branch-local restart proof showing no-`instrument_cache` restart, open-position rehydration, and reconciliation MUST be produced before merge.
9. **Q9 — full removal, expanded scope:** delete both definition sites (`services/nautilus/instruments.py:90` + `live_instrument_bootstrap.py:146`) and all runtime uses; remove `SecurityMaster.resolve_for_live` cold-miss canonicalization entirely (Maintainer); replace CLI path (`cli.py:799`) with **direct provider/root normalization + IB qualification, NOT registry lookup** (Simplifier — circularity catch); delete `_ROLL_SENSITIVE_ROOTS` dead code (Safety) if no remaining consumer.
10. **Q10 — stronger replacement strategy:** migrate behavior tests to registry semantics, delete cache-only/helper-isolation tests, AND add a structural guard that fails if runtime paths still import or reference legacy cache/helper code. Sides with Safety + Simplifier + Maintainer over a lighter fixture-port approach because main regression risk is architectural backsliding, not just behavior drift.

#### Consensus Points

- `nautilus_instrument_json` removed; Redis/Nautilus is the runtime cache.
- Hard cutover, NOT shimmed.
- Orphaned/malformed legacy rows must NOT be silently skipped.
- Pre-cutover registry-coverage check for active deployments + explicit restart proof on this branch.
- `canonical_instrument_id()` cannot survive in runtime resolution; live cold-miss fallback + round-trip validation removed.

#### Blocking Objections (mandatory in plan)

- **Simplifier:** CLI replacement for IB refresh seeding MUST use direct provider/root normalization + qualification — NOT registry lookup (circular).
- **Maintainer:** remove `SecurityMaster.resolve_for_live` cold-miss canonicalization entirely.
- **Safety:** pre-cutover gate validating every active `LiveDeployment.canonical_instruments` entry against the registry; abort with operator-action hint on any miss.
- **Safety:** branch-local paper restart drill with open positions, Redis rehydration, reconciliation after subprocess is off `instrument_cache`.
- **Pragmatist:** documented `pg_dump` checkpoint + zero-skew rollout sequence (code rewrite + migration + worker restarts as one coordinated cut).
- **Maintainer:** `MarketHoursService` fully moved off `instrument_cache` before table drop, proven with tests.

#### Minority Report

- **Steward** — Wanted child tables for `trading_hours` and `ib_contracts` with effective-window discipline (mirroring `instrument_aliases`). **Overruled:** combined hard cut + full helper removal adopted; child-table redesign deferred to its own future schema change if multi-row temporal history becomes a real requirement. `ib_contract_json` dropped (no post-migration owner).
- **Safety** — Wanted split (cache first, canonical-helper after one paper-week soak). **Overruled on sequencing** (preserves half-migrated architecture across release boundary); **safety mechanisms adopted in full** (preflight validation, restart drill, dead-code removal).
- **Pragmatist** — Wanted split + retain `ib_contract_json` on definitions. **Overruled on split + JSON retention** (rollback convenience doesn't justify dead parallel state); **operational discipline adopted** (preflight, `pg_dump`, zero-skew rollout documentation).
- **Simplifier** — Wanted helper-first split + skip+log orphans + flagged CLI circularity. **Helper-first + skip+log overruled** (silent registry gaps not acceptable); **CLI circularity objection adopted in full** — CLI uses direct provider/root normalization + IB qualification, NOT registry lookup.
- **Maintainer** — Combined cutover + remove live cold-miss canonicalization + stronger structural guard test + validate registry invariants (not only deployment rows). **Mostly adopted.** Partial deferral: deployment-row preflight still required (active rows carry `canonical_instruments`) IN ADDITION TO registry invariants.

#### Missing Evidence (resolve in plan / TDD execution)

- Branch-local restart proof: paper deploy with open positions rehydrates from Redis + reconciles correctly with no `instrument_cache` access.
- Preflight result for the 7 active `LiveDeployment` rows: whether every listed symbol resolves through the registry today.
- Actual orphan/malformed-row profile in `instrument_cache` — does cleanup reveal a historical writer bug?
- CLI's direct provider/root normalization + IB qualification fully covers the futures-month cases formerly handled by `canonical_instrument_id()`.
- Structural proof / test output: no remaining runtime imports/references to legacy cache/helper code after cutover.

#### Next Step

Write **Implementation Plan: Single-PR Hard Cutover from `instrument_cache` to Registry with Full `canonical_instrument_id()` Removal**.

---

(Council verdict ratified; awaiting user confirmation before `/prd:create`.)
