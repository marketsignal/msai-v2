# Research: Instrument Cache → Registry Migration

**Date:** 2026-04-27
**Feature:** Retire `instrument_cache` Postgres table + delete `canonical_instrument_id()` runtime helper; promote `instrument_definitions` + `instrument_aliases` to single source of truth.
**Researcher:** research-first agent

---

## Scope of This Brief

This is an internal-mechanics migration. The PRD (`docs/prds/instrument-cache-registry-migration.md` v1.0, council-ratified 2026-04-27) pins the _what_ (Q1–Q10 binding decisions). This brief researches the _how_ — current state of the libraries the migration touches, so the design phase doesn't build on stale assumptions about Alembic, SQLAlchemy 2.0, NautilusTrader's Redis-backed Cache, IB futures qualification, Postgres 16 ALTER TABLE, ruff custom rules, or stdlib `zoneinfo`.

The PRD's binding decisions in §9 are NOT re-litigated here. If a research finding contradicts a binding decision (e.g. a Nautilus regression that means we MUST keep `nautilus_instrument_json`), it is surfaced as a **Blocking Risk** for Pablo to decide whether to re-run council — not silently incorporated.

---

## Libraries Touched

| Library / API                         | Our Version (pyproject.toml)     | Latest Stable                                          | Breaking Changes for Us                            | Source                                                                                                                                                                                                                    |
| ------------------------------------- | -------------------------------- | ------------------------------------------------------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alembic                               | `>=1.14.0`                       | `1.18.4`                                               | None impacting this PR                             | [Alembic docs](https://alembic.sqlalchemy.org/en/latest/cookbook.html) (2026-04-27)                                                                                                                                       |
| SQLAlchemy (asyncio)                  | `>=2.0.36`                       | `2.0.x` line stable; 2.1 in development                | None                                               | [SQLA 2.0 docs](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html) (2026-04-27)                                                                                                                               |
| NautilusTrader                        | `>=1.222.0` (with `[ib]` extras) | `1.225.0` (rel. 2026-04-06)                            | None impacting Cache+IB durability                 | [v1.224 release notes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.224.0); [develop RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md) (2026-04-27) |
| `nautilus_trader[ib]` (ibapi adapter) | matched via extras               | `ibapi 10.43` (upgraded in v1.223)                     | None — load_contracts API stable                   | [Nautilus IB integration docs](https://docs.nautilustrader.io/integrations/ib.html) (2026-04-27 — note: docs.nautilustrader.io was 502 once during fetch; data triangulated via web search + nightly mirror)              |
| PostgreSQL                            | `16` (Compose-pinned)            | `18` (current); 16 still LTS                           | None — `ADD COLUMN JSONB NULL` semantics unchanged | [PostgreSQL ALTER TABLE](https://www.postgresql.org/docs/current/sql-altertable.html) (2026-04-27)                                                                                                                        |
| Python `zoneinfo` (stdlib)            | Python 3.12                      | Python 3.13/3.14 stable; module unchanged              | None                                               | [zoneinfo docs](https://docs.python.org/3/library/zoneinfo.html) (2026-04-27)                                                                                                                                             |
| ruff                                  | `>=0.8.0`                        | `0.6+` line; `flake8-tidy-imports` rules `TID2xx` ship | None — `banned-api` covers our use case            | [ruff settings](https://docs.astral.sh/ruff/settings/) (2026-04-27)                                                                                                                                                       |
| Typer                                 | `>=0.15.0`                       | Stable                                                 | None                                               | (no separate fetch — no API touched)                                                                                                                                                                                      |

---

## Per-Library Analysis

### 1. Alembic — data migration + DROP TABLE in one revision (Tier 1 — decision-changing)

**Versions:** ours `>=1.14.0`, latest `1.18.4`. No version-bump pressure.

**Question:** PRD US-001 acceptance criterion #1 says "New Alembic migration upgrades `instrument_cache` → registry then drops the table." Should the data copy + DDL DROP be in ONE revision or CHAINED? What's the rollback story?

**Current best practice (from docs):**

The Alembic cookbook explicitly cautions: _"Alembic migrations are designed for schema migrations. The nature of data migrations are inherently different and it's not in fact advisable in the general case to write data migrations that integrate with Alembic's schema versioning model."_ It then notes: _"downgrades are difficult to address since they might require deletion of data, which may even not be possible to detect."_

For our use case, however, the migration is single-direction by PRD design (US-001 acceptance criterion: "Alembic downgrade is schema-only — recreates the empty `instrument_cache` table; data restoration requires `pg_dump`"). That matches the cookbook's guidance: do NOT promise reversible data round-trips. Document the loss-of-data on downgrade in the migration docstring + runbook.

The DDL primitives are stable:

- `op.drop_table("instrument_cache")` — emits `DROP TABLE`
- `op.execute(...)` — for the row-by-row INSERT … ON CONFLICT DO NOTHING data copy. PR #44's `_upsert_definition_and_alias` (using the `compute_advisory_lock_key` blake2b path + `pg_insert(...).on_conflict_do_nothing()`) is the project's existing idempotent pattern. We reuse it from Python in the migration's `upgrade()` body, NOT from raw SQL — re-importing the model is brittle in migrations (PR #44 plan-review iter-3 surfaced this), so the cleanest pattern is hand-rolled `pg_insert(...)` over reflected `Table` objects via `op.get_bind()`.

**Single vs chained revisions:** the council overruled the split (Q1 = combined; Q7 = hard cutover). The combined-revision risk is operator forgets `pg_dump` and rolls back, losing data. Mitigations the PRD already encodes:

1. Loud docstring at top of migration ("DROP IS DESTRUCTIVE — pg_dump first").
2. Preflight script (US-002) aborts before `alembic upgrade head` if any active deploy can't resolve through registry.
3. Operator runbook explicitly lists `pg_dump` as a required step.

**Sources:**

1. [Alembic Cookbook — Conditional Migration Fragments](https://alembic.sqlalchemy.org/en/latest/cookbook.html) — accessed 2026-04-27. Quote: _"It's not in fact advisable … to write data migrations that integrate with Alembic's schema versioning model."_
2. [Alembic Operation Reference — drop_table](https://alembic.sqlalchemy.org/en/latest/ops.html) — accessed 2026-04-27. `op.drop_table()` accepts `if_exists` flag for defensive drops.
3. Project's own pattern: PR #44 Alembic `b6c7d8e9f0a1` (CHECK relaxation) and PR #45 Alembic `c7d8e9f0a1b2` (`SymbolOnboardingRun`). Both use `op.execute(text(...))` for in-revision data ops.

**Design impact:** Keep the migration single-revision per council Q1=combined. Order inside `upgrade()`: (a) `op.add_column("instrument_definitions", sa.Column("trading_hours", JSONB, nullable=True))` first (additive, fast) → (b) loop over reflected `instrument_cache` rows, hand-roll `INSERT … ON CONFLICT DO NOTHING` against `instrument_definitions` + `instrument_aliases` (UPSERT idempotency mirrors PR #44 + #45) + UPDATE `instrument_definitions.trading_hours` from each migrated row → (c) `op.drop_table("instrument_cache")`. Wrap in a single transaction so partial migration on failure is atomic-rolled-back (Alembic does this by default). Migration docstring must lead with `WARNING — DROP IS DESTRUCTIVE; pg_dump before upgrade`.

**Test implication:** Round-trip test under `tests/integration/test_alembic_migrations.py` using the existing `_run_alembic` subprocess harness: (a) run `alembic upgrade head`, seed N representative `instrument_cache` rows in the _previous_ HEAD revision via the model class (the model still exists at that revision), (b) `alembic upgrade head`, (c) assert `instrument_cache` table is gone, (d) assert N corresponding rows in `instrument_definitions` + `instrument_aliases` with `trading_hours` carried forward and `nautilus_instrument_json` + `ib_contract_json` discarded. Plus: a fail-loud test where one of the `instrument_cache` rows has malformed `canonical_id` (no venue suffix) → migration aborts with operator-readable message naming the PK (per US-001 edge-case row 1).

---

### 2. NautilusTrader Redis-backed Cache durability (Tier 1 — decision-changing)

**Versions:** ours `>=1.222.0`, latest `1.225.0` (rel. 2026-04-06).

**Question:** Confirm that `Cache.add_instrument(inst)` + a fresh `Cache` against the same Redis with the same `TraderId` reliably re-loads the instrument. PRD US-005 (branch-local restart drill) depends on this. Are there breaking changes / new gotchas in `>=1.222`?

**Current best practice:**

The Cache concept doc and the Releases changelog confirm:

- `CacheConfig(database=DatabaseConfig(type="redis"))` is the supported persistence path. Cache stores instruments, accounts, orders, positions, and bar references in Redis when this is set.
- `buffer_interval_ms` controls write batching: `100ms` is the documented "good compromise"; `0` or `None` is the write-through mode (gotcha #7 in the project's own `nautilus.md`). Production should use write-through; backtest should not write at all.
- `bulk_read_batch_size` was added between v1.220 and v1.225 as a new option for batched Redis reads; non-breaking, doesn't affect us.

The Releases changelog shows NO instrument-persistence regressions or breaking changes between v1.222 and v1.225 affecting `Cache.add_instrument` durability. The IB adapter was upgraded to `ibapi 10.43` in v1.223 (a non-breaking upgrade for our usage).

**Project-internal evidence:** `tests/integration/test_cache_redis_instrument_roundtrip.py` (per CHANGELOG line 510, shipped 2026-04-17 with PR #32) already proves `Cache → Redis → Cache` round-trip durability. That test will catch a future regression.

**Local source corroboration:** the project's `nautilus.md` rule file documents the buffered-cache gotcha (#7) and the "use `cache.database = redis` (or postgres) in production" architectural rule (#7). No code-side change is required — `live_node_config.py:309, 546` and `projection/position_reader.py:140` already wire the Redis backend (per PRD §5 Dependencies — confirmed present).

**Sources:**

1. [NautilusTrader Cache concept docs](https://nautilustrader.io/docs/latest/concepts/cache/) — accessed 2026-04-27. Documents `CacheConfig(database=DatabaseConfig(type='redis'))` + `buffer_interval_ms` semantics.
2. [GitHub develop RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md) — accessed 2026-04-27. Confirms `bulk_read_batch_size` added; no instrument-persistence regression v1.220 → v1.225.
3. [v1.224 release notes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.224.0) — accessed 2026-04-27. No CacheConfig breaking change.
4. Project-internal: `.claude/rules/nautilus.md` gotcha #7 + architectural rule #7 — read as reference.

**Design impact:** No code changes required to Cache wiring. The migration removes the Postgres-backed `_read_cache`/`_write_cache` paths from `SecurityMaster` (per US-003 acceptance criterion); Nautilus's Redis-backed `Cache` is the runtime instrument cache from PR #32 onward. The migration simply removes a parallel path that was masking the dependency. **No impact** on the binding decision tree — confirms PRD §5 Dependencies are present.

**Test implication:** US-005 branch-local restart drill is the gold-standard test. Plus: `test_cache_redis_instrument_roundtrip.py` (existing) MUST stay green throughout the migration. If a future PR breaks Cache durability, the drill will catch it AND that test will fail.

---

### 3. IB Gateway `InteractiveBrokersInstrumentProvider` for futures qualification (Tier 1 — decision-changing)

**Versions:** ours via `nautilus_trader[ib]>=1.222.0`, latest `ibapi 10.43` shipped in Nautilus v1.223.

**Question:** US-004 acceptance criterion: `cli.py` `instruments refresh --provider interactive_brokers` futures-month handling must do "direct root-symbol normalization + IB qualification, NOT a registry lookup" (Simplifier's circular-CLI catch). Concretely: replace the `canonical_instrument_id("ES", today=today)` call at `cli.py:799` with a path that hands an `IBContract(secType="FUT", symbol="ES", lastTradeDateOrContractMonth="202606", exchange="CME", currency="USD")` to the IB provider and lets IB resolve the actual canonical (`ESM6.CME`) at qualification time.

**Current best practice (Nautilus IB integration docs + project code):**

The IB integration docs document `IBContract(secType='FUT', exchange='CME', symbol='ES', lastTradeDateOrContractMonth='20240315')` as the canonical "Individual Futures" pattern; the provider's `load_contracts: FrozenSet[IBContract]` config knob is the way to seed the provider. Quote from Nautilus docs: _"Individual Futures: IBContract(secType='FUT', exchange='CME', symbol='ES', lastTradeDateOrContractMonth='20240315')"_.

There is also a `secType='CONTFUT'` for "continuous futures (which automatically roll)" — which is interesting but NOT applicable here. The PRD's US-004 says we need the _concrete_ monthly contract (`ESM6.CME`) so live-deploy strategies subscribe to the right Nautilus instrument. CONTFUT abstracts the roll, which would be a different design decision (out of scope).

**Failure mode (per docs):** Nautilus's IB provider "skips with a warning" on **unsupported** sec types (`WAR`, `IOPT`); for unqualifiable contracts (e.g. expired month, bad exchange), the docs are silent — but the project's existing `IBQualifier` adapter (`backend/src/msai/services/security_master/ib_qualifier.py`) already raises typed errors via `ib_async`. Existing behavior: an unqualifiable IB contract → exception bubbles up → CLI prints operator-readable error.

**Project-internal precedents we can reuse rather than re-invent:**

- `live_instrument_bootstrap.py:_current_quarterly_expiry()` (lines ~98–130 region) — pure function, builds `YYYYMM` for the front-month given today's date. Holiday-adjusted via IB's own resolver (the bug fixed in PR #37 commit `e5afb7e` — futures use `%Y%m` not `%Y%m%d` so IB resolves holiday-adjusted Juneteenth/etc).
- `live_instrument_bootstrap.py:_FUT_MONTH_CODES` map — static `{1: "F", 2: "G", ...}` quarterly-month-letter mapping. NOT an oracle of canonical IDs; just letter codes.
- `_es_front_month_local_symbol()` — one fixed-symbol helper.

The CLI replacement for `canonical_instrument_id()` should:

1. Take a bare root symbol (e.g. `"ES"`) and an asset class hint (the CLI already collects `--symbols`).
2. Build an `IBContract(secType="FUT", symbol="ES", lastTradeDateOrContractMonth=_current_quarterly_expiry(today_utc), exchange="CME", currency="USD")`.
3. Hand that to the existing `IBQualifier.qualify_contract(...)` path (already wired through `SecurityMaster._upsert_definition_and_alias` in the registry-warm-path).
4. Use the qualifier's returned `localSymbol` (`ESM6`) + `exchange` (`CME`) to form the alias string `ESM6.CME` — **but never as a "canonical helper" — this is the IB provider speaking, not a hardcoded oracle**.

The non-futures CLI path (equity / FX / ETF) bypasses the futures-month dance entirely — those `IBContract` shapes need only `(secType=STK, symbol, exchange="SMART", primaryExchange, currency)` and IB's own qualification fills in the rest.

**Sources:**

1. [Nautilus IB integration docs](https://docs.nautilustrader.io/integrations/ib.html) — accessed 2026-04-27 (via web search; docs.nautilustrader.io returned 502 once during fetch — content corroborated through search snippet + nightly mirror gitbookhub.com). Documents `IBContract` shape for FUT and `load_contracts` config.
2. [v1.224 release notes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.224.0) — accessed 2026-04-27. Confirms `IBContract` API stable v1.220 → v1.225; `ibapi` upgraded to 10.43 in v1.223 (non-breaking for our use).
3. Project-internal: `live_instrument_bootstrap.py` lines 90–270 — futures quarterly-expiry helper, `phase_1_paper_symbols`, `build_ib_instrument_provider_config` already in production.
4. Project-internal: `feedback_e5afb7e` lessons (CHANGELOG "Done cont'd 9") — `ib_qualifier.py` futures use `%Y%m` not `%Y%m%d`; this fix already merged 2026-04-20.

**Design impact:** Keep `_current_quarterly_expiry()`, `_FUT_MONTH_CODES`, and the IB qualification path. **DELETE only** `canonical_instrument_id()` (both definition sites) AND the closed-universe `phase_1_paper_symbols` helper if it's no longer used by anything but dead code (verify during plan phase — not for this brief). The CLI rewrite is a small refactor: extract a new private helper `_build_ib_contract_for_root(root: str, asset_class: str, today: date) -> IBContract` in `cli.py` (or in a new module under `services/security_master/` if shared with another caller) that produces the right `IBContract` shape based on `asset_class` ∈ `{equity, etf, future, forex, ...}`, then hands it to the qualifier. **The closed-universe `if-chain` (AAPL → NASDAQ, SPY → ARCA, ES → ESM6.CME) is replaced by per-asset-class `IBContract` factories that defer the venue resolution to IB itself** — exactly Simplifier's circular-CLI catch demand.

**Test implication:** New CLI test (US-006 acceptance criterion) exercises `msai instruments refresh --symbols ES --provider interactive_brokers` end-to-end against a stubbed `IBQualifier` that returns a synthetic `ESM6` contract → assert registry has the new alias `ESM6.CME` linked to root `ES`. Plus: parametrized over `[ES, AAPL, SPY, EUR/USD]` to cover the four asset classes the closed-universe oracle used to handle. The per-root qualification path is what's being tested, NOT a hardcoded venue map.

---

### 4. SQLAlchemy 2.0 model deletion + `Base.metadata` semantics (Tier 2 — mechanism-confirming)

**Versions:** ours `>=2.0.36`, latest `2.0.x` line stable; 2.1 in development.

**Question:** When we delete `models/instrument_cache.py` + `from msai.models import InstrumentCache` re-exports, does `Base.metadata.tables` still contain `instrument_cache`? Does `Base.metadata.create_all()` (used by some test fixtures) still try to create the legacy table?

**Current best practice:**

In SQLAlchemy 2.0 declarative, **`Base.metadata.tables` is populated only by classes that successfully imported AND inherited from `Base`**. Once `InstrumentCache` is deleted from `models/instrument_cache.py` AND removed from `models/__init__.py`'s re-export list, the class is never created, the `__tablename__ = "instrument_cache"` declaration never executes, and `Base.metadata.tables` does not contain the entry. `create_all()` will not attempt to create the table. The DDL DROP in the Alembic migration is the only way the table goes away in production.

**Project-internal pattern (from PR #43 mypy cleanup):** SQLAlchemy 2.0 `Mapped[X]` annotations evaluate at class construction; if a model with `Mapped[X]` is imported but `X` is not yet a class (forward ref), it can raise `NameError` at startup. This is unrelated to deletion, but a cautionary tale: **search for all `from msai.models.instrument_cache import …` and `from msai.models import InstrumentCache` references project-wide before deleting the file** so we don't trip the import-time crash.

**Sources:**

1. [SQLAlchemy 2.0 — Table Configuration with Declarative](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html) — accessed 2026-04-27. Documents that `__tablename__` declaration registers Table on `Base.metadata` at class-construction time.
2. [SQLAlchemy 2.1 — ORM Mapped Class Overview](https://docs.sqlalchemy.org/en/21/orm/mapping_styles.html) — accessed 2026-04-27. Forward-compat reference; current 2.0 patterns continue to work.
3. Project-internal: PR #43 mypy cleanup CHANGELOG — `Mapped[X]` forward-ref resolution gotchas; same pattern applies to deletion (orphan imports must be cleaned).

**Design impact:** Use a pre-deletion sweep: `rg -n "InstrumentCache\|instrument_cache" backend/src backend/tests` to enumerate all references before deleting `models/instrument_cache.py`. Each non-test reference is cleaned per the PRD's US-003 + US-004 acceptance criteria. Test fixtures using `Base.metadata.create_all()` are safe — the table simply doesn't exist anymore, no fixture breakage from that side. The migration tests need to use the model at the _previous_ alembic revision (where it still exists); standard `_run_alembic` subprocess harness pattern from PR #32.

**Test implication:** Add a "model registry hygiene" assertion to the new structural-guard test (US-006): `"instrument_cache" not in Base.metadata.tables` after the migration runs in tests. This catches a future regression where someone re-adds the model class without re-adding the table to the schema.

---

### 5. PostgreSQL 16 `ALTER TABLE … ADD COLUMN JSONB NULL` performance (Tier 2 — mechanism-confirming)

**Versions:** Postgres 16 (Compose-pinned). Latest is 18 — no version migration in this PR.

**Question:** Is `ALTER TABLE instrument_definitions ADD COLUMN trading_hours JSONB NULL` metadata-only (fast, no table rewrite) on Postgres 16? Are there GIN-index implications if `trading_hours` later needs content queries?

**Current best practice:**

Confirmed metadata-only on Postgres 11+: _"Adding a column with no default is a metadata-only operation in PostgreSQL and does not rewrite the table."_ JSONB columns are no different from any other type in this respect — the rewrite trigger is `NOT NULL + DEFAULT volatile_expression`, which we don't use. Our column is `nullable=True` with no default, so the migration runs in milliseconds even on tables with millions of rows.

GIN indexing on JSONB content is supported (`CREATE INDEX … USING GIN (trading_hours)`) but **not needed for this PR** — `MarketHoursService.load()` reads the full JSONB blob per-row by primary key (or by `instrument_uid` foreign key), never queries by content. If a future PR needs to filter by `trading_hours -> 'timezone'`, that's its own design call.

**Sources:**

1. [PostgreSQL 16 ALTER TABLE docs](https://www.postgresql.org/docs/current/sql-altertable.html) — accessed 2026-04-27. Confirms nullable + no-default ADD COLUMN is metadata-only.
2. [Web search: "PostgreSQL 16 ADD COLUMN JSONB nullable metadata only"](https://0xhagen.medium.com/understanding-the-performance-difference-in-adding-columns-in-postgresql-d46eaaa3f64a) — accessed 2026-04-27. Triangulates the no-rewrite claim.
3. Project-internal: PR #41 alembic migration `z4x5y6z7a8b9` (added `series JSONB NULL` + `series_status` to `backtests`) — "metadata-only on Postgres 16" precedent. CHANGELOG documents it.

**Design impact:** Migration is fast + non-blocking even with active DB connections. The maintenance window already takes the stack offline (compose down → migrate → compose up), so this is a "free" correctness — the migration phase is bounded by row count of `instrument_cache` (currently ~5–20 rows on Pablo's dev stack per CONTINUITY.md), not by Postgres ALTER TABLE rewrite time.

**Test implication:** No new perf test required. The alembic round-trip test (US-006) implicitly exercises the ALTER TABLE path under testcontainers Postgres 16.

---

### 6. ruff custom rules / structural guard for forbidden-symbol enforcement (Tier 2 — mechanism-confirming)

**Versions:** ours `ruff>=0.8.0`. The `flake8-tidy-imports` rule family ships in-tree.

**Question:** PRD US-006 needs a structural guard test that fails if any future code reintroduces `canonical_instrument_id`, `InstrumentCache`, `_read_cache`, etc. into `backend/src/`. What's the cleanest mechanism — extend the existing AST-walking test, or use ruff's `flake8-tidy-imports.banned-api`, or `rg`-shell-out via pre-commit?

**Current best practice:**

Three patterns are viable; trade-offs differ:

**Option A — Extend the existing AST-walker test** (`tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py`).

- Pros: project-internal precedent (already shipped 2026-04-20); fully Python-native; reasons-out-of-source-tree allowlist is explicit (the migration's Alembic file can be hardcoded as exempt); fast (< 100ms in current form); no new dependency.
- Cons: must broaden coverage from 2 hardcoded files to all of `backend/src/msai/` (recursive walk); needs explicit allowlist to skip the migration file's docstring.
- Pattern: `pathlib.Path("backend/src/msai").rglob("*.py")` minus `backend/src/msai/alembic/**` (or specifically the migration file containing `instrument_cache` in its docstring).

**Option B — ruff `flake8-tidy-imports.banned-api`** in `pyproject.toml`.

- Pros: lint-time enforcement (CI-blocking); applies to imports project-wide via `[tool.ruff.lint.flake8-tidy-imports]`.
- Cons: targets module-level imports (`from msai.models import InstrumentCache`), NOT arbitrary symbol references inside docstrings/comments/string literals; can't ban a symbol _defined_ in the same project (only imports of it).
- Useful for: explicitly banning `from msai.models import InstrumentCache` if someone adds the class back; will NOT catch `_read_cache` defined as a local method.

**Option C — `rg`-shell-out from a pytest test or pre-commit hook.**

- Pros: simplest semantics — literal substring search; covers comments + docstrings.
- Cons: false-positive risk (e.g. blog post in `docs/` mentioning the symbol); slower than AST walk; harder to scope precisely.

**Recommendation: Option A as the primary mechanism (extends precedent).** Option B as a complementary lint-side guard for the import case (cheap to add). Option C avoided.

**Sources:**

1. [ruff settings — flake8-tidy-imports](https://docs.astral.sh/ruff/settings/) — accessed 2026-04-27. Documents `banned-api` config key + TID2xx rule family.
2. [ruff GitHub — flake8-tidy-imports](https://github.com/astral-sh/ruff-pre-commit) — accessed 2026-04-27. Pre-commit integration.
3. Project-internal: `tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` — existing AST-walker pattern shipped 2026-04-20.

**Design impact:** Replace existing test (per US-004 acceptance criterion) with a broader Option-A AST-walker that walks all of `backend/src/msai/`, with an explicit allowlist for the migration file. The forbidden-symbol list is per US-006 acceptance criterion: `canonical_instrument_id`, `InstrumentCache`, `_read_cache`, `_upsert_cache`, `_instrument_from_cache_row`, `_ROLL_SENSITIVE_ROOTS`. Optionally add ruff `banned-api` entries in `pyproject.toml` as a belt-and-suspenders guard for the import case (cheap; no separate test file needed).

**Test implication:** The structural-guard test IS the test. Run it in `tests/unit/structure/` (existing convention). Performance budget: < 1 second total (current AST-walker hits ~50ms; broader scope may grow to ~200ms for ~200 files).

---

### 7. Python `zoneinfo` + `MarketHoursService` rewrite (Tier 3 — note-and-move-on)

**Versions:** Python 3.12 (project pin via `pyproject.toml: requires-python = ">=3.12,<3.14"`).

**Question:** Any deprecations / breaking changes in `zoneinfo` between Python 3.12 → 3.13 that affect `MarketHoursService`'s rewrite to read from `instrument_definitions.trading_hours` instead of `instrument_cache.trading_hours`?

**Current best practice:** No changes. `zoneinfo.ZoneInfo("America/New_York")`, `datetime.now(tz=ZoneInfo("..."))`, and the day-of-week / time-of-day comparisons stay identical. The PRD US-003 rewrite is a 1-line query change (`select(InstrumentDefinition.trading_hours).where(...)` instead of `select(InstrumentCache.trading_hours).where(...)`); the parsing logic in `_parse_hhmm`, `_is_in_window`, and the day-name array `_DAY_NAMES` is unchanged.

**Sources:**

1. [Python 3 zoneinfo docs](https://docs.python.org/3/library/zoneinfo.html) — accessed 2026-04-27. Module unchanged 3.9 → 3.14.
2. Project-internal: `services/nautilus/market_hours.py` — current implementation explicitly notes "We do NOT call `pytz` — the stdlib zoneinfo is the right choice" (lines 19–21).

**Design impact:** None — strict 1-line query swap. Existing tests should continue to pass once the model is changed.

**Test implication:** Add ONE new test (per US-006 acceptance criterion: "One new market-hours test against the new `instrument_definitions.trading_hours` column"). Use existing test fixtures + factory pattern; the asserts are unchanged from the legacy test.

---

### 8. Typer CLI for the rewritten `msai instruments refresh` (Tier 3 — note-and-move-on)

**Versions:** ours `typer>=0.15.0`. No API touched by this PR — only the body of one command callback.

**Question:** Any concerns? **No.** The typer surface (`@app.command()`, `Argument`, `Option`, `--symbols`, `--provider`) is stable; the rewrite is purely in the function body, replacing `canonical_instrument_id(root)` with `_build_ib_contract_for_root(root, asset_class, today)`.

**Sources:** N/A — no API change.

**Design impact:** None.
**Test implication:** Standard CLI test via `CliRunner` (existing pattern in `tests/unit/cli/`).

---

## Not Researched (with justification)

- **Frontend / React / Next.js / shadcn-ui** — backend-only PR. UI is deferred per PRD non-goal #4.
- **`pytest-asyncio`** — already heavily used; no decision-changing research needed for this PR.
- **`pydantic-settings`** — not touched (no new env var).
- **`structlog` / `prometheus_client`** — no new metrics or logs introduced by the cutover itself; only existing metrics surface (preflight script already uses standard structlog patterns).
- **`databento` SDK + `tenacity`** — not touched (this PR doesn't go through the bootstrap path).
- **`fakeredis` + `testcontainers`** — already in dev deps; standard usage in alembic round-trip tests.
- **`arq`** — not touched (no new background jobs).

---

## Open Risks

These are **risks**, not blocking findings — none contradict the PRD's binding decisions in §9. They're the things to watch during plan + implementation.

1. **OQ-003 `instrument_cache` orphan profile is unmeasured.** Pablo (or a Phase 3 plan task) needs to actually `psql` into the dev DB and run `SELECT * FROM instrument_cache LIMIT 50` before drafting the plan. If any rows have malformed `canonical_id` (no `.venue` suffix), missing `asset_class`, or invalid JSONB in `trading_hours`, US-001 says the migration fails loud (good). But if there are _many_ corrupt rows, the operator may need a separate cleanup script before running the migration. This brief cannot resolve OQ-003 — Phase 3 must.

2. **Same-day alias rotation CHECK trap (now resolved on main).** Migration `b6c7d8e9f0a1` (PR #44, 2026-04-24) relaxed `ck_instrument_aliases_effective_window` from strict `>` to `>=` so same-day rotations are safe. **The migration in this PR must run AFTER `b6c7d8e9f0a1` is at HEAD** (it is, on `main` since 2026-04-24). If we somehow base off an older commit, this becomes a P0. Quick check: `alembic current` should show `c7d8e9f0a1b2` or descendant. (Worktree was created from `9f93fcc` post-#45 — so we're safe.)

3. **`InteractiveBrokersInstrumentProvider` "skips with a warning" on unsupported sec types** (per Nautilus docs). The CLI rewrite must handle this — if an operator runs `msai instruments refresh --symbols WEIRD --provider interactive_brokers` for a sec type IB doesn't qualify, we want a clear error, not a silent NOOP. The existing `IBQualifier` adapter raises typed errors for failed qualification (PR #37 verified live), so this is mostly a fail-loud-to-CLI plumbing concern. Plan Phase 3 should pin behavior with a unit test for "unqualifiable-symbol → CLI exits non-zero with operator hint".

4. **Worker stale-import refresh after deletion of `models/instrument_cache.py`.** Per `feedback_restart_workers_after_merges.md`: workers cache imported Python modules at startup. Volume mounts update files but the running interpreter keeps the OLD module dict until `./scripts/restart-workers.sh` runs. The PRD US-001 acceptance criterion already calls this out; runbook must list the worker restart explicitly. Risk: forgetting the restart yields confusing "AttributeError on legacy attribute" errors that look like a migration bug.

5. **Nautilus `InteractiveBrokersInstrumentProvider` futures qualification — the docs are silent on the failure mode for unqualifiable contracts** (vs. unsupported sec types). The project's own code path (via `IBQualifier`) raises typed errors via `ib_async`. Confidence: HIGH that the existing code handles this correctly (PR #37 lived through 6 real-money drills + Juneteenth holiday fix). But the new CLI path that REPLACES `canonical_instrument_id()` should be E2E-tested with a known-bad symbol (e.g. `--symbols XXXFAKE --provider interactive_brokers`) to confirm fail-loud behavior.

6. **`docs.nautilustrader.io` returned 502 once during this research session.** The Nautilus docs site has been intermittently flaky during this session. Some IB qualification details were corroborated via web search snippet + nightly mirror (gitbookhub.com) rather than direct fetch. If the plan or design phase needs deeper Nautilus internals, a re-fetch may be required (or read venv source under `.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/`).

7. **Codex unavailable for cross-review of this brief.** Per the workflow rules, Codex is the second reviewer in plan-review and code-review loops. The brief was authored by a single agent without a Codex pass. Plan-review (Phase 3.3) is the natural place to catch any blind spots in this research. **Mitigation:** Pablo or a council pass should re-validate the IB-qualification design choice (US-004) when the plan is being written, since that's the one place this brief made an architectural judgment call (delete the closed-universe oracle, use `IBContract` factories per asset-class).

---

## Summary for the Caller

| Metric                                   | Count                                                                                                                                       |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Libraries researched                     | 8 (3 Tier-1 decision-changing + 3 Tier-2 mechanism-confirming + 2 Tier-3 noted)                                                             |
| Design-changing findings                 | **3** (Alembic single-revision pattern, IB futures qualification via `IBContract` factories, structural-guard preferred mechanism Option A) |
| Open risks flagged                       | **7**                                                                                                                                       |
| Findings contradicting binding decisions | **0**                                                                                                                                       |

**Key finding:** The Nautilus `InteractiveBrokersInstrumentProvider` `IBContract(secType="FUT", lastTradeDateOrContractMonth="YYYYMM")` API is the canonical "give me the front-month futures contract" path — IB's own qualifier resolves `ES` + `202606` → `ESM6.CME` at qualification time (holiday-adjusted), so the CLI replacement for `canonical_instrument_id()` becomes per-asset-class `IBContract` factories that hand control to IB rather than a hardcoded venue map (closes Simplifier's circular-CLI catch and lets US-004 ship without re-inventing the wheel).

Sources:

- [Alembic Cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html)
- [Alembic Operation Reference](https://alembic.sqlalchemy.org/en/latest/ops.html)
- [NautilusTrader Cache concept docs](https://nautilustrader.io/docs/latest/concepts/cache/)
- [NautilusTrader develop RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md)
- [NautilusTrader v1.224.0 release](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.224.0)
- [NautilusTrader IB integration docs](https://docs.nautilustrader.io/integrations/ib.html)
- [SQLAlchemy 2.0 declarative](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html)
- [PostgreSQL 16 ALTER TABLE](https://www.postgresql.org/docs/current/sql-altertable.html)
- [Python zoneinfo docs](https://docs.python.org/3/library/zoneinfo.html)
- [ruff settings — flake8-tidy-imports](https://docs.astral.sh/ruff/settings/)
- [PyPI nautilus_trader](https://pypi.org/project/nautilus-trader/)
