# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                                                            |
| --------- | -------------------------------------------------------------------------------- |
| Command   | /new-feature instruments-refresh-ib-path                                         |
| Phase     | 5 — Quality Gates + Manual Drill                                                 |
| Next step | Phase D manual paper drill (operator + paper IB Gateway) + Phase E quality gates |

### Checklist

- [x] Worktree created
- [x] Project state read
- [x] Plugins verified
- [x] PRD created
- [x] Research artifact produced (`docs/research/` — via research-first agent)
- [ ] Design guidance loaded (if UI)
- [x] Brainstorming complete (design doc: `docs/plans/2026-04-18-instruments-refresh-ib-path-design.md`)
- [x] Approach comparison filled (council discussion Round 2 resolved the 6 design Qs; one remaining arch question resolved via Codex second opinion)
- [x] Contrarian gate passed — 5-advisor council fired (standalone mode via /council) with Codex chairman synthesis
- [x] Council verdict: hybrid answers per chairman recommendation; 3 blocking objections adopted, 2 scope-creep additions overruled
- [x] Plan written (`docs/plans/2026-04-18-instruments-refresh-ib-path.md`)
- [x] Plan review loop (4 iterations) — PASS. Codex iter 1: 6 P1 + 4 P2 + 2 P3. Iter 2: 1 P1 + 4 P2 + 1 P3. Iter 3: 1 P1. Iter 4: clean.
- [x] TDD execution complete (13 code/test commits across 4 batches, all red→green→refactor)
- [x] Code review loop (1 iteration) — Codex + pr-review-toolkit:code-reviewer + 3 simplify agents in parallel; 1 P2 (Codex: dotted-symbol normalization) + 1 P2 (reuse+efficiency: third_friday_of duplicate) + 4 P1 + 3 P2 (quality: comment cleanup) — all fixed in commit eaaa446. pr-review-toolkit:code-reviewer returned PASS (only P3 nits).
- [x] Simplified — 3 parallel simplify agents (reuse/quality/efficiency) ran and their P1/P2 findings folded into eaaa446.
- [x] Verified (tests/lint/types) — verify-app agent PASS: 1406→1407 unit tests (+ dotted-alias test) + 232 integration pass + ruff clean on touched files + mypy at main baseline.
- [x] E2E use cases designed (Phase 3.2b) — 6 user stories in PRD with Gherkin scenarios
- [x] E2E verified — N/A via verify-e2e agent: manual Phase D paper drill on 2026-04-18 20:30 UTC executed all 5 use cases against running paper IB Gateway (DUP733213). Results persisted in docs/CHANGELOG.md Phase D section. Opt-in `pytest.mark.ib_paper` smoke file at `tests/e2e/test_instruments_refresh_ib_smoke.py` provides automated regression surface for future re-verification.
- [ ] E2E regression passed (Phase 5.4b)
- [ ] E2E use cases graduated to tests/e2e/use-cases/ (Phase 6.2b)
- [ ] E2E specs graduated to tests/e2e/specs/ (Phase 6.2c — if Playwright framework installed)
- [ ] Learnings documented (if any)
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

### Feature scope

Complete the deferred `msai instruments refresh --provider interactive_brokers` path. Currently (claude-version/backend/src/msai/cli.py:747-756) that branch hard-fails with a deferral message. The blocker is that `Settings` lacks `ib_request_timeout_seconds`, `ib_instrument_client_id`, and a paper/live port selector needed to build an `IBQualifier`. Deliverables: (a) extend `Settings` with the missing fields + env-var aliases, (b) add a small IBQualifier factory, (c) wire it into `instruments_refresh` to call `SecurityMaster.resolve_for_live`, (d) tests + docstring updates. Touches IB wiring — mind Nautilus gotcha #3 (unique client_id) + gotcha #6 (paper/live port consistency).

## Done

- Hybrid merge PR#3 merged (2026-04-13): 18 tasks, 99 files, ~15K lines
- Docker Compose parity PR#4 merged (2026-04-13): 12 gaps fixed, all 10 containers running
- IB Gateway connected: 6 paper sub-accounts verified (DFP733210 + DUP733211-215, ~$1M each)
- Databento API key configured
- Phase 2 parity backlog cleared 2026-04-15: PR #6 portfolio, #7 playwright e2e, #8 CLI sub-apps, #9 QuantStats intraday, #10 alerting API, #11 daily scheduler tz — all merged after local merge-main-into-branch conflict resolution (1147 tests on final branch)
- First real backtest 2026-04-15 14:01 UTC: AAPL.NASDAQ + SPY.ARCA Databento 2024 full year, 258k bars, 4,448 trades, QuantStats HTML report via `/api/v1/backtests/{id}/report`. Core goal from Project Overview met.
- Alembic migration collision fixed: PR #6 + PR #15 both authored revision `k9e0f1g2h3i4`; portfolio rechained to `l0f1g2h3i4j5` (commit 3139d75).
- Bug A FIXED (PR #16, 2026-04-15 19:27 UTC): catalog rebuild detects raw parquet delta via per-instrument source-hash marker; legacy markerless catalogs purged + rebuilt; basename collisions across years + footer-only rewrites both bump the hash; sibling bar specs survive purge. 5 regression tests + 2 Codex review iterations (P1 + 3×P2 all addressed).
- Live drill on EUR/USD.IDEALPRO 2026-04-15 19:30 UTC verified PR #15 trade persistence end-to-end: BUY @ 1.18015 + SELL (kill-all flatten) @ 1.18005 both wrote rows to `trades` with correct broker_trade_id, is_live=true, commission. ~376 ms kill-to-flat. Two minor follow-ups noted: side persists as enum int (1/2) not string (BUY/SELL); realized_pnl from PositionClosed not extracted into trades.
- Multi-asset live drill 2026-04-15 19:36-19:45 UTC FAILED to produce live fills on AAPL/MSFT/SPY/ES — see Now section. Demonstrated only EUR/USD reliably produces fills with current paper account/config.
- Phase 2 #4 council (5 advisors + chairman): rejected verbatim Option A (867 LOC) and framed Option B (300 LOC); mandated paper-IB kill-all drill as go/no-go gate
- Phase 2 #4 drill executed (2026-04-15 04:00 UTC): exposed 3 P0 live-stack bugs blocking any `/live/start` (profile-gate, supervisor silent-fail, IB host/port drift)
- Phase 2 #4 — live trade persistence merged (PR #15): broker_trade_id column + partial unique dedup + ON CONFLICT DO NOTHING path from OrderFilled → trades; audit row mismatch now visible (Codex review P1+P2 both addressed)
- Live-stack kill-all drill PASSED 2026-04-15 05:37: EUR/USD.IDEALPRO paper BUY filled → /kill-all → SELL reduce_only filled → PositionClosed in 187 ms. Layer 3 (SIGTERM + manage_stop=True) verified.
- Live-stack sprint complete 2026-04-15 06:00 UTC — all 3 P0s fixed in separate branches ready for PR+merge:
  - P0-B `fix/live-supervisor-silent-spawn-fail` (f324f0c): LiveCommandBus.\_publish now calls ensure_group before xadd so commands don't vanish when consumer group is positioned at `$`; supervisor **main**.py configures stdlib logging.basicConfig so its logs are visible in docker logs
  - P0-C `fix/ib-gateway-env-var-drift` (6f02767): settings.ib_host/ib_port accept AliasChoices on IB_GATEWAY_HOST + IB_GATEWAY_PORT_PAPER env names
  - P0-A `fix/live-supervisor-default-profile` (08b34a9): /live/start returns 503 fast when no supervisor consumer is registered (vs silent 504 timeout)

## Done (cont'd)

- ES futures canonicalization merged 2026-04-16 04:35 UTC (PR #23): fixes the drill's zero-bars failure mode at the MSAI layer. `canonical_instrument_id()` maps `ES.CME` → `ESM6.CME` so the strategy's bar subscription matches the concrete instrument Nautilus registers from `FUT ES 202606`. Spawn-scoped `today` threaded through supervisor + subprocess (via `TradingNodePayload.spawn_today_iso`) closes the midnight-on-roll-day race. Live-verified: subscription succeeds without `instrument not found`. Caught a `.XCME` vs `.CME` venue bug in live testing that unit tests missed. 28 new bootstrap tests (39 total). Codex addressed 4 rounds of findings + a 5th surfaced only by the live deploy. DUP733213's missing real-time CME data subscription confirmed as the remaining upstream blocker (IB error 354) — operator action at broker.ibkr.com, not code.
- 7-bug post-drill sprint complete 2026-04-16 02:31 UTC — every offline-fixable bug from the 2026-04-15 multi-asset drill aftermath shipped to main, no bugs left behind:
  - **Bug #1** PR #17 — backtest metrics now derive from positions when Nautilus stats return NaN (3-tier fallback: stats → account snapshot → positions). Verified: win_rate=0.17, sharpe=-45.7 on AAPL/SPY 2024.
  - **Bug #2** PR #18 — `/account/health` IB probe now starts as a FastAPI lifespan background task (30s interval). Verified: `gateway_connected=true` after first probe tick.
  - **Bug #3** commit 2084423 — `READ_ONLY_API` compose default flipped to `no` so paper-trading orders submit without per-session env override (was triggering IB error 321 in 2026-04-15 drill).
  - **Bug #4** PR #19 — `PositionClosed.realized_pnl` now propagates to `trades.pnl` via new `client_order_id` linkage; subscribed to `events.position.*` in subprocess.
  - **Bug #5** PR #20 — `graduation_candidates.deployment_id` auto-links on `/live/start` so the graduation → live audit chain stays connected.
  - **Bug #6** PR #21 — `trades.side` now persists as `BUY`/`SELL` strings via `OrderSide.name` (was leaking enum int 1/2 into the DB).
  - **Bug #7** PR #22 — `claude-version/scripts/restart-workers.sh` ships ~10s worker container restart for stale-import hygiene; documented in `claude-version/CLAUDE.md`.

## Done (cont'd 2) — Portfolio-per-account-live PR #1

**All 12 plan tasks landed** (branch `feat/portfolio-per-account-live`, 11 commits: Tasks 3+4 combined atomically for forward-ref resolution). Plan-review loop passed 3 iterations clean (Claude + Codex on iter 4). Per-task subagent-driven execution with spec + quality reviews after each task — all passed.

- **Schema (Task 1, `288743c`):** Alembic migration `o3i4j5k6l7m8` creates `live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `live_deployment_strategies`; adds `ib_login_key` + `gateway_session_key`; partial unique index `uq_one_draft_per_portfolio` via `postgresql_where=sa.text(...)`. No FK cycle — active revision computed via query in `RevisionService.get_active_revision`.
- **Models (Tasks 2-6, `760500b`..`5e1ee41`):** `LivePortfolio` (TimestampMixin), `LivePortfolioRevision` (immutable, `created_at` only), `LivePortfolioRevisionStrategy` (M:N bridge, immutable), `LiveDeploymentStrategy` (per-deployment attribution bridge), `ib_login_key` + `gateway_session_key` additive columns on existing tables.
- **Services (Tasks 7-9, `a591089`, `520ad50`, `5153704`):** `compute_composition_hash` (deterministic canonical sha256 across sorted, normalized member tuples), `PortfolioService` (create + add_strategy + list_draft_members + get_current_draft; enforces graduated-strategy invariant), `RevisionService` (`snapshot` with `SELECT … FOR UPDATE` row lock for concurrency + identical-hash collapse; `get_active_revision`; `enforce_immutability` defensive guard).
- **Tests (Tasks 10-11, `24046a4`, `0572089`):** Full-lifecycle integration (`test_portfolio_full_lifecycle.py`) exercises create → add × 3 → snapshot → rebalance → second-snapshot → audit-preservation → cascade-delete paths. Alembic round-trip test (`test_o3_portfolio_schema_roundtrip`) validates upgrade + downgrade + re-upgrade using the repo's subprocess `_run_alembic` harness.
- **Polish (Task 12, `f2e125c`):** ruff + mypy `--strict` clean on the 7 new source files + 20 PR#1 files total. `TYPE_CHECKING` guards added for imports only needed at type-check time. No unit regressions (1228 still passing).

**Test totals:** 1228 unit pass · 13 new integration pass (5 PortfolioService + 6 RevisionService + 2 full_lifecycle + 1 alembic round-trip) + 199 pre-existing integration pass · ruff + mypy clean on all new files.

## Done (cont'd 3) — PR#1 quality gates

- **Simplify pass (`2f6490b`):** Reuse/Quality/Efficiency three-agent simplify found one real pattern — extracted `CreatedAtMixin` to `base.py`; applied to the 3 immutable models (revision, revision-strategy, deployment-strategy). Removed narrative PR#1-scope comment from `_get_or_create_draft_revision` docstring.
- **verify-app:** PASS. 1228 unit + 13 new integration + 199 pre-existing integration pass (2 unrelated pre-existing failures flagged). Ruff + mypy --strict clean on all PR#1 source files.
- **Code review iter-1 — 6 reviewers in parallel:** Codex CLI + 5 PR-review-toolkit agents (code-reviewer, pr-test-analyzer, comment-analyzer, silent-failure-hunter, type-design-analyzer).
  - Findings fixed in `060bc89`:
    - **Codex P1** — `add_strategy()` now acquires `SELECT FOR UPDATE` on the draft + checks `is_frozen`, preventing the race where a concurrent `snapshot()` freezes the draft mid-add and the member-insert corrupts the composition hash.
    - **Codex P1** — `compute_composition_hash` now quantizes weight to the DB `Numeric(8,6)` scale before hashing. Prevents divergence between a pre-flush hash (`Decimal("0.3333333")`) and a post-Postgres-round hash (`0.333333`).
    - **P1 (code-reviewer + pr-test-analyzer)** — partial unique index `uq_one_draft_per_portfolio` now declared inline on `LivePortfolioRevision.__table_args__`, so `Base.metadata.create_all` fixtures exercise the same invariant as the migration. Added `test_partial_index_rejects_second_draft` + `test_partial_index_allows_two_frozen_revisions`.
    - **P2 (silent-failure-hunter)** — `snapshot()` error cases split into typed exceptions under shared `PortfolioDomainError` base: `NoDraftToSnapshotError` (replaces opaque `ValueError`), `EmptyCompositionError` (new snapshot-time guard). `RevisionImmutableError` + `StrategyNotGraduatedError` now inherit the same base for unified catch blocks.
    - **P2** — docstring/code mismatch in `_get_or_create_draft_revision` rewritten to accurately describe the partial-index + `IntegrityError` contract.
    - **P2** — dropped "PR #1 of" reference from the migration docstring (CLAUDE.md rules — no caller history in code).
  - Findings fixed in `422bbca`:
    - **P1 (type-design-analyzer)** — DB-level CHECK `ck_lprs_weight_range` (weight > 0 AND weight <= 1) on `live_portfolio_revision_strategies`. New migration `p4k5l6m7n8o9`; mirrored in model `__table_args__`. Tests `test_weight_check_rejects_zero` + `test_weight_check_rejects_over_one`.

**Test totals after iter-1 fixes:** 1228 unit + 27 portfolio integration (+ 4 new from fixes) + 199 pre-existing integration. Ruff clean on all PR#1-touched files. Alembic chain now ends at `p4k5l6m7n8o9`.

## Done (cont'd 4) — Portfolio-per-account-live PRs #2–#4 merged

- **PR #29 — PR#2 semantic cutover** merged 2026-04-16. 1341 unit tests, 15/15 E2E against live dev Postgres. 2-iteration code-review loop (Codex + 5 PR-toolkit agents). Details in `docs/CHANGELOG.md`.
- **PR #30 — PR#3 multi-login Gateway topology** merged 2026-04-16.
- **PR #31 — PR#4 enforce `portfolio_revision_id` NOT NULL + deprecate legacy `/live/start`** merged 2026-04-16 (current main head 5a539f8).
- **Multi-asset drill follow-ups (PRs #24–#27)** merged 2026-04-16: WebSocket reconnect snapshot with 8-key hydration; live-stack hardening (concurrent-spawn serialization, cross-loop dispose, deployment-status sync on spawn failure); deployment.status sync on stop + typed `HEARTBEAT_TIMEOUT`; `/live/positions` empty-while-open-position fix.
- **First real-money drill on `U4705114`** 2026-04-16 14:52 UTC: AAPL BUY 1 @ $261.33 → SELL flatten @ $262.46 via /kill-all. Live-verified PR #21 (side="SELL"), PR #19 (pnl=-0.88), PR #24 (3 trades in snapshot). Net drill cost: ~$0.88 + $2.01 commissions.

## Done (cont'd 5) — db-backed-strategy-registry PRD + plan (this session, 2026-04-17)

- **Worktree + branch** `feat/db-backed-strategy-registry` at `.worktrees/db-backed-strategy-registry` (from main 5a539f8).
- **Research streams (parallel):**
  - Explore agent mapped Nautilus venv (`InstrumentProvider`, IB/Databento adapters, Cache, `ParquetDataCatalog`) + claude-version current state.
  - Codex CLI ran independent first-principles research on Nautilus best practices.
  - Two Codex findings overturned Explore's initial claims (both verified directly): `ParquetDataCatalog.write_data()` DOES treat `Instrument` as first-class (`parquet.py:294-299`); Nautilus Cache DB DOES persist Instruments via `CacheConfig(database=...)` (`cache/database.pyx:583`).
  - Outcome: codex-version's 605-LOC `NautilusInstrumentService` partially reinvents Nautilus's own persistence. MSAI's table becomes a thin control-plane (no `Instrument` payload column).
- **5-advisor Council** invoked for the MIC-vs-exchange-name venue-scheme decision:
  - Personas: Maintainer (Claude), Nautilus-First Architect (Claude), UX/Operator (Claude), Cross-Vendor Data Engineer (Codex), Contrarian/Simplifier (Codex). Chairman: Codex xhigh.
  - Tally: 3 advisors voted Option B (exchange-name); both Codex advisors independently converged on a THIRD option (stable logical UUID PK + alias rows).
  - Nautilus-First Architect caught a factual error in the original framing: Databento loader does NOT emit `XCME` — it emits `GLBX` or exchange-name.
  - Chairman synthesis: **hybrid — third option at schema layer + Option B at runtime alias layer**. Minority report preserved: both Codex dissents adopted at the durable layer.
- **4 Missing-Evidence items resolved by Claude research** (after user accepted hybrid, corrected "no Polygon"): IB options route via `SMART`/listing exchange preserved in `contract_details.info` → listing/routing split stays on schema; split-brain extent is small (~7 docstrings + 26 test fixtures, runtime already uses `.CME`); no Parquet rewrite needed (MSAI storage is symbol-partitioned); cache-key invalidation on format change is safe (one-time re-warm).
- **PRD v1.0 written** at `docs/prds/db-backed-strategy-registry.md`. 8 user stories (US-001–US-008), Gherkin scenarios + acceptance criteria + edge cases. Non-goals explicit (no Polygon, no wholesale MIC migration, no UI form generator, no options code paths, no bulk backfill, no cross-adapter canonicalization outside `SecurityMaster`).
- **Discussion log** saved at `docs/prds/db-backed-strategy-registry-discussion.md` (full research streams + Q&A rounds + council verdict + missing-evidence resolutions). Status: Complete.
- **Implementation plan v1.0** written at `docs/plans/2026-04-17-db-backed-strategy-registry.md`: 9 phases, 25 tasks, TDD sub-steps, exact file paths + full code bodies + commit messages. New Alembic revision `v0q1r2s3t4u5` revises current head `u9p0q1r2s3t4`.

## Done (cont'd 6) — db-backed-strategy-registry PR shipped (2026-04-17)

- **PR #32 merged** to main at `a52046f` (squash). 35 commits on branch collapsed: 22 TDD task commits (T1–T20 via subagent-driven-development), 10 code-review fixes (F1–F10), 1 simplify commit (S1–S6 bundled), 2 docs commits.
- **Plan-review loop:** converged after 5 iterations (scope-back to backtest-only + 15 mechanical fixes).
- **Code-review loop:** 1 iteration multi-reviewer parallel (6 PR-review-toolkit + Codex); all P0/P1 landed.
- **Post-PR review (Codex bot):** 2 P1 findings on the open PR, both fixed in-branch before merge:
  - `8f5f943` — close previous active alias before inserting new one (futures-roll / repeated-refresh race). Test: `test_security_master_resolve_live.py` AAPL.NASDAQ → AAPL.ARCA roll.
  - `415a858` — raise `AmbiguousSymbolError` on cross-asset-class raw-symbol match (schema uniqueness is `(raw_symbol, provider, asset_class)`; `resolve_for_{live,backtest}` don't pass `asset_class`). Test: `test_instrument_registry.py` SPY as equity + option.
- **Worktree cleaned** (`.worktrees/db-backed-strategy-registry` removed, remote + local branch deleted).
- **Pre-existing main dirty tree preserved**: CLAUDE.md (E2E Config), `claude-version/docker-compose.dev.yml` (new IB_PORT + TRADING_MODE env vars), 38 codex-version in-progress files (portfolio-per-account port), tests/e2e fixtures, IB-Gateway runtime data all restored. Stale `CONTINUITY.md` + `docs/CHANGELOG.md` discarded in favor of origin/main versions. Safety branch: `backup/pre-pr32-cleanup-20260417`.
- **Workers restarted** (`./scripts/restart-workers.sh`) to pick up new security_master modules; `GET /health` on :8800 returns 200.

## Done (cont'd 7) — resolve_for_backtest honors start_date (2026-04-18)

- **Fix scope:** `SecurityMaster.resolve_for_backtest` (service.py) — threaded existing `start: str | None` kwarg through both warm paths so historical backtests get the alias active during the backtest window, not today.
  - Path 2 (dotted alias): `registry.find_by_alias(..., as_of_date=as_of)`
  - Path 3 (bare ticker): replaced `effective_to IS NULL` filter with full window predicate `effective_from <= as_of AND (effective_to IS NULL OR effective_to > as_of)`
- **3 new integration tests** — `test_security_master_resolve_backtest.py`: dotted-alias-historical, bare-ticker-historical, bare-ticker-today-default regression guard. All 6 tests in file pass; 122 security_master/backtest-scope tests pass total.
- **Quality gates:**
  - Code review (pr-review-toolkit): CLEAN (P3-only nits)
  - Codex CLI: stalled on both attempts, killed; workflow permits single-reviewer
  - Simplify (3 parallel agents — reuse/quality/efficiency): all CLEAN (P3-only)
  - Verify: ruff + mypy clean on my changed lines; in-scope tests pass; pre-existing full-suite failures (30/78) confirmed present on main, untouched by this fix
  - E2E: N/A — fix is only observable via state that can't be arranged through sanctioned public-interface channels (alias windows have no public CRUD)
- **Solution doc:** `docs/solutions/backtesting/alias-windowing-by-start-date.md`.
- **Closes** PR #32 CHANGELOG "Known limitations discovered post-Task 20, limitation #2".

## Done (cont'd 8) — Stale post-PR#29 test cleanup (2026-04-18)

Cleanup of 30 failures + 78 errors that were pre-existing on main, all rooted in stale tests that predated PR#29/#30/#31's schema changes.

- **Root causes addressed:** (1) PR#29 dropped 5 cols from `live_deployments`; (2) PR#30 added NOT NULL `ib_login_key` on `LiveDeployment` and `gateway_session_key` on `LiveNodeProcess`; (3) PR#31 enforced `portfolio_revision_id NOT NULL` and deprecated `/api/v1/live/start`; plus an unrelated OHLC-invariant bug in synthetic bar generator and a stale `order_id_tag` assertion that didn't expect PR#29's order-index prefix.
- **New fixture helper:** `tests/integration/_deployment_factory.py::make_live_deployment` — seeds `LivePortfolio → LivePortfolioRevision → LiveDeployment` with all NOT NULL cols populated + unique slug/signature per call. Accepts ORM instances or IDs.
- **Files migrated to factory (9):** test_audit_hook, test_heartbeat_monitor, test_heartbeat_thread, test_live_node_process_model, test_live_start_endpoints, test_live_status_by_id, test_order_attempt_audit_model, test_process_manager, test_trading_node_subprocess. Plus test_portfolio_deploy_cycle got `ib_login_key` kwarg.
- **Tests deleted:** test_live_deployment_stable_identity.py (6 tests of v9 intermediate design — replaced by PortfolioDeploymentIdentity). 4 obsolete 1.1b tests in test_alembic_migrations.py. 9 tests in test_live_start_endpoints.py targeting the deprecated `/api/v1/live/start` (returns 410 Gone).
- **Assertion updates:** test_alembic_migrations backfill test now pins intentional-empty-config + intentional-empty-instruments behavior (r6m7n8o9p0q1 line 92). drops_legacy_columns test updated for `portfolio_revision_id NOT NULL` (PR#31). test_live_status_by_id instruments assertion updated to `[]` (endpoint returns backward-compat empty list post column drop). test_parity_config_roundtrip order_id_tag assertion updated to `"0-<slug>"` format.
- **Fix:** test_parity_determinism.\_write_synthetic_bars now derives high/low from max/min(open, close) so Nautilus `Bar.__init__` invariant holds.
- **Scope:** claude-version only. Test-only cleanup — no production code modified. 16 files changed (1 helper added, 1 file deleted, 14 patched).

## Now

- **Active workflow:** `/new-feature instruments-refresh-ib-path` on branch `feat/instruments-refresh-ib-path` (worktree).
- **Phase 4 batches 1-4 complete:** Phases A (foundation) + B (CLI impl) + C1 (opt-in smoke) + B4/B5 (docs) all shipped locally (9 commits on branch: 38db52b, af4f031, fe8bfbe, 52410cb, 32b63d8, 6fd2b75, be829a5, ea1bbef, cb13042, ab69af7, 3b8a782, 104a594).
- **Docker stack restarted from worktree (2026-04-18 20:27):** containers now bind-mount the worktree's claude-version/backend/src. Backend healthy. IB Gateway mid-login cycle — waiting for paper session to settle before running the manual drill (Phase D).

## Next

1. **Phase E — quality gates** (in progress): full unit suite + lint + typecheck on all changed files.
2. **Phase F — state sync:** finalize CONTINUITY + CHANGELOG at final state.
3. **Phase G — `/finish-branch`:** push branch, open PR, handle reviews, merge.

### Phase D drill results (2026-04-18 20:30 UTC)

All 5 drills pass against paper IB Gateway (DUP733213 @ ib-gateway:4004 via docker compose exec backend):

- D2 happy path: exit 0 in 2.6s, AAPL → AAPL.NASDAQ, 1 def + 1 alias row written
- D3 idempotent re-run: 1/1 counts unchanged
- D4 gateway-down fast-fail: 7.7s exit non-zero + operator hint
- D5 port/account mismatch: 1.3s exit non-zero, no IB attempted
  - **`msai instruments refresh` for plain symbols** — Databento path works; IB path skipped.
