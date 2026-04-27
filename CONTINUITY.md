# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value |
| --------- | ----- |
| Command   | none  |
| Phase     | —     |
| Next step | —     |

## Prior workflow checklist (archived — PR #45 symbol-onboarding SHIPPED 2026-04-25)

- [x] Worktree created at `.worktrees/symbol-onboarding` off `09e956e` (main @ PR #44 merge + CONTINUITY cleanup)
- [x] Project state read
- [x] Plugins verified — `/prd:discuss` + `/prd:create` + `/council` all functional; `/council` dispatched 5 advisors + Codex chairman successfully this session.
- [x] PRD discuss complete — `docs/prds/symbol-onboarding-discussion.md` closed 2026-04-24. 10 questions in 4 groups routed to `/council` (5 advisors + Codex xhigh chairman). Verdict: all 10 answered + 2 binding contract corrections (pin #3 scoping + `/api/v1/universe` deprecation). Minority Report preserved (Simplifier's ONE-watchlist + no-cost-preview overruled; Scalability Hawk's circuit-breaker + cancel-now overruled; Contrarian's pin-#3 fatal-flaw ACCEPTED; Maintainer's `asset_universe` model-fracture ACCEPTED).
- [x] PRD created — `docs/prds/symbol-onboarding.md` v1.0. 10 user stories (US-001..US-010: YAML manifest, async API job, per-symbol progress, preflight cost estimate + ceiling, partial-batch semantics, window+provider-scoped `backtest_data_available`, explicit repair action, `msai symbols onboard` CLI, `msai symbols status` CLI, `/api/v1/universe` deprecation). 11 explicit non-goals (UI deferred, cancel, rollback, silent auto-retry, per-strategy manifests, single watchlist.yaml, DB-backed CRUD, 1s bars, multi-user, cron scheduler, new providers). 6 success metrics (cold-start < 3min, 0 manual SQL, idempotent re-apply, scoped readiness correctness, budget ceiling enforcement, partial-batch clarity). 8 open questions routed to Phase 2 research-first agent (Databento `metadata.get_cost` accuracy, `/api/v1/universe` live-caller count, `asset_universe.resolution` usage grep, `OnboardingJob` persistence, `CoverageSnapshot` compute strategy, Prometheus metrics set, deprecation grace period, `trailing_5y` sugar expansion).
- [x] Research artifact produced — `docs/research/2026-04-24-symbol-onboarding.md` (339 lines). 7 Tier-1/2 libraries deep-researched + 3 Tier-3 noted + 1 explicit-skip (Next.js/React — UI deferred). **6 design-changing findings:** (1) `Historical.metadata.get_cost()` has no published error band but agrees BYTE-FOR-BYTE with `get_range` for fully-historical completed windows — `estimate_confidence` becomes a declared classification (`high` when `end < today-1d` and no ambiguous/continuous symbols, `medium` otherwise), NOT a computed interval; (2) US-004 endpoint shape: use **separate `POST /api/v1/symbols/onboard/dry-run` endpoint**, NOT `?dry_run=true` query param — cleaner OpenAPI + TS codegen; (3) `OnboardingJob` = dedicated Postgres table (arq Redis records expire after completion, can't model partial-success for operator reporting); (4) `CoverageSnapshot` = on-the-fly Parquet directory scan for v1; flip to cached table only if >500-symbol watchlists appear; (5) `trailing_5y` expanded client-side via `dateutil.relativedelta`, default `end=today-1d` (NOT `today` — dodges PR #44's nightly-publication-window gotcha that caused the E2E FAIL_BUG); (6) idempotency `_job_id` MUST use `hashlib.blake2b`, NOT Python's built-in `hash()` — mirrors PR #44's `compute_advisory_lock_key` (PYTHONHASHSEED randomization would break cross-process idempotency). **Net-new dep:** add **PyYAML 6.0.2** to `backend/pyproject.toml`; pin `yaml.safe_load` at every call site (never `yaml.load`). **SDK deprecation:** do NOT pass `mode=` to `metadata.get_cost` — deprecated at SDK 0.65.0. 11 open risks flagged (publication-window divergence, continuous-futures cost surprise, cross-provider ambiguity, etc.).
- [x] Design guidance loaded — **N/A**: backend-only feature. UI deferred to post-v1 (PRD non-goal #1). No `/ui-design` skill needed.
- [x] Brainstorming complete — Phase 3.1 done in-session. Orchestrator topology was the one genuinely open design decision (everything else pinned by PRD + research). Claude proposed 3 approaches (monolithic / parent+child fan-out / chained tasks) and initially recommended Approach 2.
- [x] Approach comparison filled — see `## Approach Comparison` section above. Approach 1 chosen; Approach 2 scored worse on every fixed axis (Complexity H vs L, Time-to-Validate H vs L, User/Correctness Risk H vs L). Cheapest falsifying test was `cat IngestWorkerSettings.max_jobs` — `=1` killed Approach 2's parallelism claim in <30 min.
- [x] Contrarian gate passed (council) — 2026-04-24 standalone `/council` run, 5 advisors + Codex xhigh chairman. Claude's initial Approach 2 recommendation was **materially wrong** on 3 false premises: (1) PR #40 is NOT N-child fan-out precedent, (2) `Semaphore(3)` parallelism is fiction against `IngestWorkerSettings.max_jobs=1`, (3) "~50 LOC overhead" was off by an order of magnitude. Council OVERRULED Claude 4-of-5 (only Scalability Hawk was CONDITIONAL-on-Approach-2, and even then with 4 infra mitigations Claude hadn't specified).
- [x] Council verdict: **Approach 1** (single arq entrypoint `run_symbol_onboarding`, `_onboard_one_symbol()` seam for future parallelism, phase-local bounded concurrency ONLY in bootstrap phase, ingest/IB strictly sequential, `asyncio.wait_for(120s)` on IB, 100-symbol API cap, 3 Prometheus metrics, 4 binding renames: `SymbolOnboardingRun` / `symbol_states` / `cost_ceiling_usd` / `request_live_qualification`). Minority Report preserved: Scalability Hawk's CONDITIONAL-on-Approach-2 (dedicated queue + fire-and-forget parent + IB timeouts + 4 metrics) — topology overruled, safety parts ADOPTED into Approach 1 (`asyncio.wait_for(120s)` + 3 of 4 metrics; `queue_depth` dropped since no dedicated queue in v1). Renames applied to PRD; Approach-1-only constraints added to PRD §5.
- [x] Plan written — `docs/plans/2026-04-24-symbol-onboarding.md` (3,981 lines, 16 tasks T0–T15). Full code bodies + TDD steps + commit messages per task. 6 E2E use cases (UC-SYM-001..006) in T15. Inline self-review PASSED: spec coverage (all 10 US + 9 architectural constraints mapped), placeholder scan (none), type consistency (enums + method signatures + schema field names unified across tasks), scope check (single-feature, appropriately sized). Awaiting user review before plan-review loop 3.3.
- [x] Plan review loop (4 iterations — PASS 2026-04-24) — productive convergence: iter-1 16 findings (1 P0 queue-deadlock + 7 P1 + 6 P2 + 2 P3) → iter-2 6 (0 P0 + 4 P1 + 2 P2) → iter-3 2 (0 P0 + 1 P1 + 1 P2) → iter-4 CLEAN 0/0/0/0 both reviewers. Each iter narrowed in-scope. Mini-council on iter-1 P0 (5 advisors + Codex chairman): Option A + 3 binding constraints (inline ingest helper, no child arq job, persistent per-symbol error envelope). Plan v1 (3,981 lines) → v2 → v3 → v4 (~5,150 lines, 18 tasks incl. T6a ingest-helper extraction + T8-prime `_error_response` promotion). Loop closed. Ready for Phase 4 TDD.
- [x] TDD execution complete — All 18 tasks (T0–T15 + T6a + T8-prime) implemented via subagent-driven development. T0–T5 committed (`6441b64` … `9ad9119`); T6a/T13/T8-prime/T6/T7/T8/T9/T10/T11/T12/T14/T15 uncommitted on disk per workflow-gate hook (per `feedback_workflow_gate_blocks_preflight_commits.md`). Total ~50 unit/integration tests added; all green.
- [x] Code review loop (2 iterations — PASS 2026-04-25) — productive convergence: iter-1 (Codex + 3 pr-review-toolkit + verify-app) found 3 P0 + 12 P1 + 10 P2 + 3 P3 = 28 blocking findings; 12-block fix-pass landed cleanly (66/66 affected tests pass, ruff + mypy clean). iter-2 (verify-app + Codex) found 0 P0 + 1 P1 + 1 P2 + 1 P3 = 3 narrow findings (duplicate-symbol JSONB collision, CLI Decimal serialization, double-counted histogram); all fixed inline. 28→3 trajectory.
- [x] Simplified — 3-agent parallel sweep (reuse / quality / efficiency). 5 P1 fixes applied: (a) `_CONTINUOUS_FUTURES_RE` dedup → use existing `is_databento_continuous_pattern` from PR #44; (b) strip "council-ratified", plan-task-IDs, dates from orchestrator + worker docstrings (project comment-hygiene rule); (c) extract `_get_databento_client()` module-level singleton in `api/symbol_onboarding.py` so `/dry-run` and `/onboard` ceiling-check share one client; (d) add bucketing-trade-off docstring to `estimate_cost`. 39/39 affected tests pass; ruff + mypy clean.
- [x] Verified (tests/lint/types) — verify-app iter-2 PASS: 2109/2109 effective tests + 11 skipped + 16 xfailed (2 pre-existing flakes from PR #41 unchanged); ruff clean across `src/`; mypy `--strict` 0 errors across 181 source files (was 7 errors in `api/symbol_onboarding.py` at iter-1 — Block 8 fix verified); alembic head `c7d8e9f0a1b2`.
- [x] E2E use cases designed (Phase 3.2b) — 7 UCs (UC-SYM-001..007) drafted in plan T15 + at `tests/e2e/use-cases/instruments/symbol-onboarding.md` (intent → setup → steps → verification → persistence per `.claude/rules/testing.md`).
- [x] E2E verified via verify-e2e agent (Phase 5.4) — Report at `tests/e2e/reports/2026-04-24-symbol-onboarding.md`. **Verdict PARTIAL: 4 PASS + 3 SKIPPED_INFRA**. UC-SYM-001 (SPY 1-month happy path) PASS — real Databento ingest end-to-end, registry row + Parquet coverage + readiness all green. UC-SYM-002 (cost ceiling preflight) PASS after mid-run FAIL_BUG fix: `_get_databento_client()` returned MSAI's `DatabentoClient` wrapper but cost estimator needs the real `databento.Historical` SDK (the `.metadata.get_cost(...)` attribute lives on the SDK, not the wrapper). Fixed inline + UC re-ran PASS. UC-SYM-004 (window-scoped readiness, Contrarian pin-#3) PASS — `null` (no window) → `full` (covered) → `gapped` (with `missing_ranges`). UC-SYM-007 (idempotency) PASS — POST-1 → 202 `pending`; POST-2 → **200** `in_progress` (same run_id, real state, not stale "pending"); POST-3 → **200** `completed`. UC-SYM-003 SKIPPED_INFRA (Databento ambiguity is non-deterministic; unit tests cover the path). UC-SYM-005 + UC-SYM-006 SKIPPED_INFRA (RUN_PAPER_E2E + broker compose profile opt-in).
- [x] E2E regression passed (Phase 5.4b) — N/A: this PR's surface (new `/api/v1/symbols/*` router + new arq task + new model + new alembic migration `c7d8e9f0a1b2`) does not modify any surface underpinning prior graduated UCs (`backtests/auto-ingest`, `backtests/failure-surfacing`, `backtests/results-charts-and-trades`, `strategies/config-schema-form`, `live/registry-backed-deploy`, `instruments/databento-registry-bootstrap`). Full-suite regression via verify-app (2109/2109 effective pass) confirms no regression. T8-prime `_error_response → error_response` promotion preserved 50/50 backtests + instruments tests.
- [x] E2E use cases graduated to tests/e2e/use-cases/ (Phase 6.2b) — 7 UCs at `tests/e2e/use-cases/instruments/symbol-onboarding.md` (already in canonical location; PASS UCs ratified by verify-e2e report mtime > branch-off).
- [x] E2E specs graduated to tests/e2e/specs/ — N/A: backend-only feature (UI deferred per PRD non-goal #1).
- [x] Learnings documented — saved to auto-memory (`feedback_colocate_imports_with_usage_in_edits.md`).
- [x] State files updated — CONTINUITY checklist + CHANGELOG carry the full Phase 4 → Phase 6.2b narrative.
- [x] Committed and pushed — squash-merged at `3bd22bd` (57 files, +11867/-465).
- [x] PR created — [PR #45](https://github.com/marketsignal/msai-v2/pull/45) "Symbol Onboarding: API + CLI + arq orchestrator".
- [x] PR reviews addressed — squash-merged 2026-04-25 12:21 UTC.
- [x] Branch finished — worktree removed, remote + local branch deleted, main ff'd to `3bd22bd`.

### Scope seed (refined during PRD discuss)

**User vision (verbatim, 2026-04-24):** "How the user via CLI, API and UI tells msai that it wants a series of symbols at different bars (1s, 1m, 5m, etc), for example (SPY, E-mini, IWM, AAPL, all in 5 mins and 1 min)."

**Architecturally pinned decisions** (ratified with user before PRD):

1. **Storage is 1m canonical.** 5m / 10m / 30m / 1h / 1d all derive at backtest time via Nautilus `BarAggregator`. Onboarding has NO UI/API surface for "pick 5m AND 1m" — that's implicit. User's watchlist-level timeframe picker is a pure display affordance informing the strategy template, NOT an ingest multiplier.
2. **1-second bars are out of scope for v1.** Different Databento schema, different storage partition, ~60× cost. Deferred to a separate PRD once demand is real.
3. **Three readiness states** (the contract from PR #44) are the primary data model: `registered` / `backtest_data_available` / `live_qualified`. API/CLI/UI all surface these.
4. **`live_qualify: false` default** — onboarding stops at "backtest-ready" unless explicitly opted-in. Live qualification requires IB Gateway (`broker` compose profile, credentials, market-data entitlements) and is slow (per-symbol IB round-trip). Trader workflow is backtest → paper → live; the flag matches that graduation.
5. **Manifest-driven CLI** — trader's watchlist is a `.yaml` in git alongside strategies. Batch onboarding from manifest is the common case, not ad-hoc single-symbol calls.
6. **API is primary** — orchestrates existing primitives (bootstrap from PR #44 + ingest pipeline + IB refresh). CLI bypasses `_api_call`. UI surface under `/universe`.
7. **Asynchronous job** — `POST /api/v1/symbols/onboard` returns 202 + job_id; status polled via `GET /api/v1/symbols/onboard/{id}/status`. Bootstrapping 20 symbols × 5 years of minute bars is NOT a synchronous-HTTP-response workload.

**Scope seed:**

- New `POST /api/v1/symbols/onboard` API + `GET /api/v1/symbols/onboard/{id}/status` polling
- New `msai symbols onboard --manifest watchlist.yaml` + `msai symbols status` CLI
- New `/universe` Next.js page with 3-state matrix + coverage-gap drawer per row
- Orchestrator service composes bootstrap (#44) + ingest + optional IB refresh
- Queue-backed (arq) so user doesn't block on a 10-minute backfill

**Out of scope (defer-again candidates):**

- Second-level bars (1s, tick)
- Crypto, options, non-USD FX
- Automatic re-ingest on coverage drift (the coverage-check is read-only in v1)
- Strategy auto-association (operator still picks strategies separately)

**Key open questions for PRD discuss:**

- Should the onboard job also trigger Nautilus catalog rebuild, or leave that to the existing on-demand catalog loader? (PR #16 autonomy contract may already cover this.)
- Coverage-gap detection granularity — daily? monthly? (affects the UI drawer data shape.)
- Batch size — 50 symbols per manifest (matches bootstrap cap), or allow chunking for bigger universes?
- What happens mid-run if one symbol fails? Continue others? All-or-nothing? (bootstrap's 207 pattern + continue-others is the precedent.)
- How does the UI surface partial progress in real-time — WebSocket reconnect-friendly (PR #24 pattern), or polling?
- Cost display — is the UI expected to show "estimated Databento cost for this batch: ~$2.40"? (affects whether we pre-call `metadata.list_datasets`.)

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

## Done (cont'd 9) — Live-path wiring onto registry shipped (2026-04-20)

- **PR #37 merged** to main at `29dbe9b` (squash). 8 commits on branch collapsed: 1 main implementation (`0d3799d`), 1 E2E report (`fffb5ea`), 1 PRD/research/graduated-cases (`5342ffe`), 1 pre-PR checklist (`5fe29c7`), 2 drill docs (`665417d` initial AAPL + `7cf74f1` multi-symbol extension), 1 drill-uncovered bug-fix batch (`e5afb7e`), 1 post-review fix (`be23558`).
- **Scope shipped:** pure-read `lookup_for_live(symbols, as_of_date, session)` resolver over the DB-backed registry replaces the 5-symbol closed-universe `canonical_instrument_id()` + `PHASE_1_PAPER_SYMBOLS` gate on `/api/v1/live/start-portfolio`. Council verdict ratified at `docs/decisions/live-path-registry-wiring.md` (modified Option D). Fail-fast on registry miss with operator-copyable `msai instruments refresh --symbols X` hint; structured `live_instrument_resolved` log + `msai_live_instrument_resolved_total` counter.
- **Phase gates cleared in order** (per feedback memory `feedback_all_continuity_gates_before_pr.md`): Plan-review loop (4 iters, converging) → Code-review loop (2 iters, clean) → Simplify pass (3 agents) → Verify (1728 tests, 0 fail) → E2E (5 use cases, PASS) → Real-money drill on U4705114 (6 deploys across 5 asset classes, ~$8 total) → Regression + graduation → Commit + push → PR → Codex review → merge. Two pushbacks from Pablo (N/A-rationalized E2E; PR-before-all-gates) captured as `feedback_always_run_e2e_before_pr.md` and `feedback_all_continuity_gates_before_pr.md`.
- **Drill-uncovered bug batch** (`e5afb7e`, "no bugs left behind"): (a) `ib_qualifier.py` futures use `%Y%m` so IB resolves holiday-adjusted expiry (Juneteenth ESM6 shift); (b) `_upsert_definition_and_alias` normalizes FX `raw_symbol` at storage boundary to slash form; (c) `/live/trades` accepts + applies `deployment_id: UUID` query filter. Each re-verified against the E2E path that surfaced it (feedback memory `feedback_rerun_e2e_after_bug_fixes.md`).
- **Post-merge cleanup:** worktree removed, remote + local branch deleted, main ff'd to `29dbe9b`.

## Done (cont'd 16) — Symbol Onboarding shipped (2026-04-25 / PR #45)

- **Merged to main** at `3bd22bd` — squash of the `feat/symbol-onboarding` branch (57 files, +11,867/-465). Closes PR #44 backlog item #2: operator-facing surface for declaring "onboard symbols X..N for windows W..W'" via YAML watchlist manifest, replacing the manual SQL-seed + per-symbol `instruments refresh` ritual.
- **What shipped:**
  - **5 new HTTP endpoints under `/api/v1/symbols/`**: `POST /onboard/dry-run`, `POST /onboard`, `GET /onboard/{run_id}/status`, `POST /onboard/{run_id}/repair`, `GET /readiness`. Full council-pinned idempotency contract: `SELECT FOR UPDATE` digest → fast-path 200 → enqueue first → 100ms backoff re-SELECT on `None` → 409 `DUPLICATE_IN_FLIGHT` → commit + 202; Redis-down → 503 `QUEUE_UNAVAILABLE` zero-row guarantee.
  - **`msai symbols onboard|status|repair` CLI** with `--dry-run`, `--watch`, and Decimal-precision-validated `--cost-ceiling-usd`.
  - **arq task `run_symbol_onboarding`** — sequential on existing `msai:ingest` queue (no fan-out per council Approach 1). In-process `ingest_symbols(...)` helper extracted from `run_ingest` (T6a).
  - **`SymbolOnboardingRun` table + Alembic `c7d8e9f0a1b2`** — plural table, `updated_at`, `job_id_digest` unique-indexed (blake2b not Python `hash()` — research finding #6 echoes PR #44's `compute_advisory_lock_key`), `cost_ceiling_usd Numeric(12,2)`.
  - **4-phase per-symbol pipeline** — bootstrap → ingest → coverage → optional IB qualify (with `asyncio.wait_for(120s)` from Scalability Hawk's adopted minority report) — and persistent per-symbol error envelope (T8-prime promotes `_error_response` → shared `error_response`).
  - **3 Prometheus metrics**: `msai_onboarding_jobs_total{status}` + `msai_onboarding_symbol_duration_seconds{step}` + `msai_onboarding_ib_timeout_total`.
  - **DELETE `/api/v1/universe`** (superseded — Maintainer's binding objection from iter-1 council).
  - **`watchlists/` directory** with `README.md` + `example-core-equities.yaml` template; PyYAML 6.0.2 declared dep, `yaml.safe_load` pinned at every call site.
- **Council verdict** (standalone /council 2026-04-24, 5 advisors + Codex xhigh chairman): **Approach 1** (single arq entrypoint, `_onboard_one_symbol()` seam, phase-local bounded concurrency only in bootstrap, ingest/IB strictly sequential, 100-symbol API cap, 4 binding renames: `SymbolOnboardingRun` / `symbol_states` / `cost_ceiling_usd` / `request_live_qualification`). Claude's initial Approach 2 recommendation was OVERRULED 4-of-5 on three false premises (PR #40 not N-child fan-out precedent, `IngestWorkerSettings.max_jobs=1` defeats `Semaphore(3)` parallelism claim, "~50 LOC overhead" off by an order of magnitude). Minority Report preserved: Scalability Hawk's safety parts (IB timeout + 3 of 4 metrics) ADOPTED into Approach 1; Contrarian's pin-#3 fatal-flaw + Maintainer's `asset_universe` model-fracture also ACCEPTED. Decision: ratified in `docs/prds/symbol-onboarding-discussion.md` + Approach Comparison block in archived checklist above.
- **Quality trail:**
  - Plan-review loop 4 iters (16 → 6 → 2 → CLEAN). Mini-council on iter-1 P0 queue self-deadlock: Option A + 3 binding constraints (inline ingest helper, no child arq job, persistent per-symbol error envelope). Plan v1 (3,981 lines) → v4 (~5,150 lines, 18 tasks).
  - Code-review loop 2 iters (Codex + 3 pr-review-toolkit + verify-app): iter-1 28 findings (3 P0 + 12 P1 + 10 P2 + 3 P3) → 12-block fix-pass landed cleanly → iter-2 3 narrow findings (duplicate-symbol JSONB collision, CLI Decimal serialization, double-counted histogram) all fixed inline.
  - Simplify pass: 5 P1 fixes — `_CONTINUOUS_FUTURES_RE` deduped via `is_databento_continuous_pattern` (PR #44 reuse), docstring artifact scrub, module-level `_get_databento_client()` singleton shared between `/dry-run` and `/onboard`, bucketing-trade-off docstring on `estimate_cost`.
  - verify-app: 2109/2109 effective tests + 11 skipped + 16 xfailed (2 pre-existing flakes from PR #41 unchanged); ruff clean across `src/`; mypy `--strict` 0 errors across 181 source files; alembic head `c7d8e9f0a1b2`.
  - **E2E PASS 4/7 + 3 SKIPPED_INFRA + 1 FAIL_BUG fixed in scope.** UC-SYM-001 (SPY 1-month happy path) PASS — real Databento ingest end-to-end. UC-SYM-002 (cost ceiling preflight) PASS after mid-run FAIL_BUG fix: `_get_databento_client()` returned MSAI's `DatabentoClient` wrapper but cost estimator needs the real `databento.Historical` SDK directly (the `.metadata.get_cost(...)` attribute lives on the SDK, not the wrapper). Fixed inline + re-ran PASS. UC-SYM-004 (window-scoped readiness, Contrarian pin-#3) PASS — `null` (no window) → `full` (covered) → `gapped` (with `missing_ranges`). UC-SYM-007 (idempotency) PASS — POST-1 → 202 `pending`; POST-2 → **200** `in_progress` (same run_id, real state, not stale "pending"); POST-3 → **200** `completed`. UC-SYM-003/005/006 SKIPPED_INFRA. 7 UCs graduated at `tests/e2e/use-cases/instruments/symbol-onboarding.md`.
- **New learning saved to auto memory:** [`feedback_colocate_imports_with_usage_in_edits.md`](feedback_colocate_imports_with_usage_in_edits.md) — PostToolUse ruff formatter strips "unused" imports between subagent edits. When adding `from foo import bar`, include at least one `bar()` usage in the SAME Edit call. Bit T13/T14 subagents during Phase 4.
- **Post-merge cleanup:** worktree removed, remote + local branch deleted, main ff'd to `3bd22bd`.

## Done (cont'd 15) — Databento registry bootstrap shipped (2026-04-24 / PR #44)

- **Merged to main** at `b71aad3` — squash of 2 commits on `feat/databento-registry-bootstrap` (`7a6b17a` implementation + `cd67010` post-PR Codex P2 fixes). 38 files changed, ~7600 insertions. Closes backlog item #8 (Databento-catalog-seeded cold-start registration) from the post-PR-43 backlog.
- **What shipped:** `POST /api/v1/instruments/bootstrap` + `msai instruments bootstrap` CLI. Equity/ETF/futures symbols register via Databento without an IB Gateway dependency. Databento-bootstrapped rows are **backtest-discoverable only**; live graduation still requires an explicit `instruments refresh --provider interactive_brokers` second step.
- **Scope council verdict** ratified 2026-04-23: `1b + 2b + 3a + 4a` (arbitrary on-demand CLI/API, equities+ETFs+futures — NO options/FX/cash-indexes in v1, Databento as peer provider, metered-mindful rate limiting). 7 blocking constraints. Decision doc: `docs/decisions/databento-registry-bootstrap.md`.
- **Venue-normalization sub-council** ratified 2026-04-23: Option A (normalize MIC→exchange-name at write boundary) with 3 blocking constraints — closed MIC map + fail-loud on unknown, named helper `normalize_alias_for_registry`, raw Databento venue preserved via additive `source_venue_raw` column. Contrarian's minority-report lineage critique adopted in substance.
- **New primitives:**
  - `DatabentoBootstrapService` — session-per-symbol orchestrator with `Semaphore(3)` + `asyncio.gather(return_exceptions=True)` + synthetic `UPSTREAM_ERROR` materialization on unexpected exceptions. 8 outcome types (`created` / `noop` / `alias_rotated` / `ambiguous` / `upstream_error` / `unauthorized` / `unmapped_venue` / `rate_limited`).
  - `normalize_alias_for_registry` closed MIC map (16 entries including `EPRL→PEARL`) + fail-loud `UnknownDatabentoVenueError`.
  - Typed `DatabentoError` hierarchy carrying `http_status` + `dataset` + `AmbiguousDatabentoSymbolError` with `candidates[]` contract.
  - tenacity `AsyncRetrying` (3 attempts, exponential 1–9s, 429/5xx only, 401/403 fail-fast) wrapping sync Databento SDK via `asyncio.to_thread`.
  - `compute_advisory_lock_key(provider, raw_symbol, asset_class) -> int` shared blake2b helper in `security_master.service` — byte-identical digest used by both the registry write path and the bootstrap orchestrator.
  - 2 alembic migrations: `a5b6c7d8e9f0` adds nullable `source_venue_raw String(64)`; `b6c7d8e9f0a1` relaxes `ck_instrument_aliases_effective_window` from strict `>` to `>=` so same-calendar-day rotations produce semantically-safe zero-width `[F, F)` audit rows (half-open interval contains no dates → never selected as active). Downgrade self-cleans by DELETE'ing zero-width rows before re-adding strict CHECK.
  - `msai_registry_venue_divergence_total{databento_venue, ib_venue}` counter gated on **both** (a) Databento venue disagrees with new IB venue AND (b) new IB venue differs from prior IB venue — so idempotent re-refreshes after a real migration don't re-fire.
  - Pydantic V2 cross-field invariants on `BootstrapResult` (`@dataclass(frozen=True, slots=True)` + `__post_init__`) and `BootstrapResultItem` (`model_validator(mode="after")`).
  - HTTP 200 / 207 / 422 status contract via `api/instruments.py` (uses shared `_error_response` helper from PR #41).
- **Quality trail:**
  - Plan review 3 iterations (42→21→9 findings, productive convergence).
  - Code review 2 iterations in parallel (5 pr-review-toolkit + Codex). Iter-1 landed ~40 P0/P1/P2 fixes: path-traversal via `_safe_filename` sha1, `asyncio.gather(return_exceptions=True)`, SQLAlchemyError rollback, classification race fix (pre-acquire advisory lock before SELECT), continuous-futures typed error discrimination, `_extract_venue` fail-loud, outcome-severity ranking (UNAUTHORIZED > RATE_LIMITED > UPSTREAM_ERROR), `BootstrapResult` invariants, `BootstrapResultItem` model_validator, comment scrub across 12 source files, 9 new regression tests (UNMAPPED_VENUE/UPSTREAM_ERROR/RATE_LIMITED/DATABENTO_NOT_CONFIGURED/tenacity 5xx exhaustion/live_qualified=true/unexpected-exception partial-progress/exact_id dispatch). Iter-2 closed test-pollution on `_install_fake_databento` (snapshot-tuple restore pattern), continuous-futures lock mirror, residual comment scrub.
  - Simplify pass: extracted `compute_advisory_lock_key`; deleted dup `_CONTINUOUS_FUTURES_RE` in favor of `is_databento_continuous_pattern`; `api/instruments.py` uses shared `_error_response`. ~60 LOC deleted.
  - verify-app: 2054/2054 effective pass (2 pre-existing flakes from PR #41 unchanged). ruff clean, mypy `--strict` 0/171.
- **E2E PASS (after mid-run FAIL_BUG fix):** verify-e2e agent ran 6 UCs (UC-DRB-001..006). Found a FAIL_BUG: bootstrap probed Databento with `start=today_utc_midnight` which 4xx's during the nightly window before Databento's daily publication. Fixed at `databento_bootstrap.py` by probing a 7-day historical window ending yesterday (`start = today - 7d`, `end = today - 1d`). Re-verified live: UC-001 AAPL PASS, UC-002 SPY CLI PASS (exit 0), UC-004 idempotency PASS. UC-003 FAIL_STALE (BRK.B no longer ambiguous in real Databento — ambiguity path covered by unit tests). UC-005/006 SKIPPED_INFRA (opt-in RUN_PAPER_E2E=1, IB Gateway required). 6 UCs graduated at `tests/e2e/use-cases/instruments/databento-registry-bootstrap.md`.
- **Post-PR Codex review (iter-1):** 2 P2 findings, both real bugs fixed in-branch at `cd67010` before merge. (a) `_check_live_qualified` missed `asset_class` filter — cross-asset-class symbol collisions (IB futures `ES` alongside equity `ES` bootstrap) would falsely flag `live_qualified=true`; added parameter + regression test. (b) Divergence counter re-fired on idempotent IB refresh after a real migration — reads prior IB alias now, gates increment on IB venue transition AND Databento-vs-IB mismatch; regression test confirms two consecutive IB refreshes with same BATS venue fire the counter exactly once.
- **New learnings saved to auto memory:**
  - **Databento definition-schema probes with `start=today` fail with 4xx `data_start_after_available_end` during the nightly window between UTC midnight and Databento's daily publication.** Always probe a historical window (`start=today-7d`, `end=today-1d`) for symbol-existence checks. Caught by verify-e2e agent during Phase 5.4.
  - **Fake-module `sys.modules` helpers must snapshot + restore ORIGINALS, not just drop submodules.** Dropping `databento.common.error` on test exit broke sibling tests that imported `BentoClientError` at module-load. Codex iter-2 caught this.
- **Same-day alias rotation CHECK trap** (historical reference memory): now RESOLVED via migration `b6c7d8e9f0a1`. Reference memory updated; kept as historical reference for older branches.
- **Post-merge cleanup:** worktree removed, remote + local branch deleted, main ff'd to `b71aad3`.

## Done (cont'd 14) — mypy --strict cleanup shipped (2026-04-23 / PR #43)

- **Merged to main** at `1fe65ea` (squash of 5 commits on `fix/mypy-strict-cleanup`) — 46 files, +370/-142. Closes the CI follow-up carried forward from PR #42's `continue-on-error: true` advisory gate.
- **What shipped:** 128 → 0 `mypy --strict` errors across `src/`. Mypy is now a **blocking** CI gate (step's `continue-on-error: true` removed from `.github/workflows/ci.yml`). Also ships `actionlint` as a pre-commit hook via `.pre-commit-config.yaml` — would have caught the PR #42 `hashFiles()` parse bug before the first push.
- **Error-category breakdown** (14 categories across 11 tasks M1–M11): 31 × type-arg (missing generics on `dict`/`list`/`Mapping`) · 26 × name-defined (SQLA forward refs resolved via TYPE_CHECKING imports in 12 model files) · 18 × unused-ignore stale-error-code drift (targeted `import-untyped` but runtime said `import-not-found`) · 16 × library stub overrides (nautilus, databento, pandas, sklearn, testcontainers, azure, redis, etc.) · 11 × attr-defined · 8 × valid-type · 8 × arg-type · 7 × int-type · 6 × no-any-return · misc.
- **Non-obvious technical fixes:**
  - `builtins.list[X]` pattern in `asset_universe.py` + `portfolio_service.py` where `async def list(...)` shadows the type name at class scope — `from builtins import list as _list` would also work but `builtins.list[X]` is more legible.
  - `pg_insert(Model)` directly instead of `pg_insert(Model.__table__)` — SQLA stubs type `__table__` as `FromClause` but `pg_insert` wants `TableClause`. Passing the mapped class dodges the stub mismatch.
  - `main.py:91-98` — three latent `F821`s (`Any` + `StreamRegistry` referenced without runtime import). Saved only by `from __future__ import annotations`. Would have blown up any `get_type_hints()` call. `Any` import hoisted, `StreamRegistry` under `TYPE_CHECKING`.
  - `strategy_registry.py:416` — dual `type: ignore[misc,assignment]` on `NautilusBase = None` (class-or-None sentinel pattern): `misc` suppresses "Cannot assign to a type"; `assignment` suppresses `None → type[X]`. Rationale comment added per comment-hygiene policy.
- **Quality trail:** 3-agent parallel simplify pass (reuse/quality/efficiency) caught 3 real findings — `pg_insert` parity gap (one call site still used the stub-fighting form), `[misc,assignment]` dual-code needed a rationale comment, unnecessary intermediate variables in `_run_ingest` closure. All fixed pre-merge. verify-app PASS: ruff clean · mypy --strict 0/166 files · pytest 1703/1703. CI run `24846320955` green end-to-end.
- **Follow-up deferred:** remaining CI-hardening backlog items (dependabot, pytest-xdist, coverage floor, compose smoke, security scans) still open — see "Next" > "CI follow-up backlog".

## Done (cont'd 13) — CI unblock shipped (2026-04-23 / PR #42)

- **Merged to main** at `8537ae2` (squash of 10 commits) — 43 files, +291 / −209. Started as a `/quick-fix` CI probe and escalated to `/fix-bug` mid-session after the probe surfaced drift that had been invisible while CI was broken. Codex consulted at the scope-growth decision point (`C > B >> A` — the reframe that sealed it: "this isn't a probe anymore, it's a bug-fix branch" once latent `F821`s in `main.py` were found).
- **CI was broken since post-flatten.** `.github/workflows/ci.yml:47` used `hashFiles('frontend/pnpm-lock.yaml') != ''` at `jobs.<job_id>.if` level. GitHub Actions only allows `hashFiles()` in step-level contexts, so the workflow was rejected at parse time with 0s-duration runs. Invisible pre-flatten because the workflow lived under `claude-version/.github/workflows/` which GitHub didn't detect. Ping-workflow probe validated it was a per-workflow config issue, not org policy.
- **What shipped:**
  1. Parse bug fix in `ci.yml` (hashFiles guard removed).
  2. **110 ruff errors cleaned across 13 rule categories** via per-site triage (no blanket `--unsafe-fixes`): 32 auto-safe (UP037 on SQLAlchemy models is safe under `from __future__ import annotations` — experimentally verified all model imports still resolve); 10 × B904 (`from exc` where detail surfaces the inner exception, else `from None`); 4 × SIM105 → `contextlib.suppress()`; 6 × E501 wrap; **3 × F821 latent defects in `main.py:91,95,98`** (`Any`/`StreamRegistry` referenced without import — saved only by `from __future__ import annotations`, would have blown up any `get_type_hints()` call); 44 × TC00x via `pyproject.toml` per-file-ignores for framework-introspected dirs (`models/`, `schemas/`, `api/`, `core/auth.py`, `core/database.py`) + safe TYPE_CHECKING moves in `services/` (experimentally confirmed that moving `datetime`/`UUID`/`Decimal` into TYPE_CHECKING breaks SQLAlchemy 2.0 `Mapped[...]` resolution at class construction).
  3. **Mypy 147 → 132 errors** via stub-overrides for 16 untyped libs (nautilus, databento, pandas, arq, etc.); step marked `continue-on-error: true` so remaining 132 (real code drift) don't block merges. Deferred to dedicated cleanup PR.
  4. **`pyproject.toml` pytest `pythonpath=["src"]`** — without this, CI's `uv run pytest tests/` fails with `ModuleNotFoundError: No module named 'msai'` before collecting a test.
  5. **Stale test fixture fix** in `test_coverage_still_missing_after_ingest_returns_partial_gap` — broken on main since PR #40's 7-day coverage tolerance landed (60-second gap → 15-day gap so the partial-ingest path classifies as `COVERAGE_STILL_MISSING` instead of being tolerated).
  6. **Two CI-env-only test fixes.** `setup_logging` now disables `cache_logger_on_first_use` when `ENVIRONMENT=test` (structlog's frozen-chain behavior was defeating `structlog.testing.capture_logs()` under CI's integration-before-unit test order). `test_refresh_help_documents_providers` strips ANSI codes before substring matching (CliRunner colors output differently in CI vs local shells).
  7. **CI triggers opened** — feature-branch pushes + `workflow_dispatch` now get CI signal.
- **Final CI state on branch:** ✅ frontend 59s · ✅ backend 6m29s (ruff clean + pytest 1703/1703 + mypy advisory) · ✅ ping 3s. Run `24822937903`.
- **Tooling:** `actionlint` installed via `brew install actionlint` — would have caught the `hashFiles()` parse bug before the first push. Add as a pre-commit hook in the mypy-cleanup PR.
- **Follow-up deferred:** mypy `--strict` cleanup — 132 remaining errors (31 × type-arg missing generic params on `dict`/`list`, 26 × name-defined forward refs, 11 × unused-ignore, 11 × attr-defined, 8 × valid-type, 8 × arg-type, 7 × int, 6 × no-any-return, plus misc). Once cleaned, remove `continue-on-error: true` from `ci.yml`.

## Done (cont'd 12) — Backtest results charts + paginated trade log + in-app report iframe shipped (2026-04-23 / PR #41)

- **Merged to main** at `330e56a` (squash of 4 commits: `84de2cf` Phase 1-3 artifacts + `2178f29` Histogram primitive + `1b6a092` implementation + `c96d68c` post-review fix). Closes the "UI-RESULTS-01" follow-up Pablo flagged after the PR #40 SPY live demo.
- **What shipped:** `GET /api/v1/backtests/{id}/results` now returns a canonical `series` JSONB (daily equity + drawdown + monthly returns) with a `series_status` enum (ready / not_materialized / failed). Materialization is atomic worker-side in `_materialize_series_payload`; fail-soft sets `series_status="failed"` so the run still persists. Paginated `GET /trades` (500 clamp, `(executed_at, id)` secondary sort) replaces the previous inline-in-results trades field. Signed-URL flow for in-app QuantStats iframe: `POST /report-token` mints an HMAC capability token (prod-secret guard, path-traversal via `Path.is_relative_to`, `Content-Disposition: inline` for iframe render). Frontend detail page splits into Native view (Recharts equity/drawdown + native CSS-Grid monthly heatmap + paginated TradeLog) and Full report iframe.
- **New primitives:** `Histogram` observability type (NaN-guarded, Prometheus-compatible) → `msai_backtest_results_payload_bytes` 1KB-10MB buckets observed at worker + `/results`; `report_signer.py` module (HMAC-SHA256, stateless, 40 LOC); canonical `normalize_daily_returns` dedup'd via `analytics_math.build_series_payload`; `_report_is_deliverable()` helper for eligibility parity across `/results`/`/report-token`/`/report`; `get_current_user_or_none` helper for the public-iframe `/report` path.
- **Quality trail:** Plan review 11 iters (productive convergence, architectural pivot at iter-9 when iframe auth bypass was caught + redesigned as signed-URL flow). Code review 7 iters with 6 parallel reviewers (Codex + 5 pr-review-toolkit). 3 P0 security fixes in iter-1 (prod-secret guard, path-traversal `is_relative_to`, cross-user token replay). 3 iter-exclusive Codex P1s: iter-5 iframe `Content-Disposition: inline`, iter-6 chart TZ off-by-one (`formatTickDate()`), iter-3 alembic column name. Simplify pass extracted `_error_response` helper (~50 LOC deduped). verify-app: 1701/1702 pass (1 pre-existing failure unchanged). E2E PASS 5/6 + 1 SKIPPED_INFRA; 6 UCs graduated at `tests/e2e/use-cases/backtests/results-charts-and-trades.md`.
- **Post-merge Codex review fix (`c96d68c`):** `verify_report_token` at `report_signer.py:92` — `payload_b64.encode("ascii")` sat outside the narrow decode try/except (lines 98-114); non-ASCII input bubbled as 500 instead of 401 `INVALID_TOKEN`. Fixed with `isascii()` guard at the validation boundary (base64url alphabet is ASCII-only by definition). Regression test added. Replied on inline thread.
- **Operational note:** new env var `REPORT_SIGNING_SECRET` added to `.env.example` (required in prod; empty/short values rejected at Settings init — `openssl rand -base64 48`). Migration `z4x5y6z7a8b9` is metadata-only on Postgres 16 (no table rewrite). After merge, `./scripts/restart-workers.sh` to refresh `backtest-worker` + `job-watchdog` containers.

## Done (cont'd 11) — Backtest auto-ingest on missing data shipped (2026-04-21 / PR #40)

- **Merged to main** at `43051da` — transparent self-heal pipeline: when backtest fails with `FailureCode.MISSING_DATA`, orchestrator auto-downloads data (bounded lazy, ≤10y, ≤20 symbols, no options chain fan-out) via dedicated `msai:ingest` arq queue, then re-runs. Failure envelope only surfaces when auto-heal itself fails.
- **New primitives:** `run_auto_heal` orchestrator, `AutoHealLock` (Redis SET NX EX + Lua CAS), `AutoHealGuardrails` (frozen+slots invariants), `derive_asset_class` (async registry-first with shape fallback), `SecurityMaster.asset_class_for_alias` (registry→ingest taxonomy translation), `verify_catalog_coverage` (Nautilus-native with 7-day edge-gap tolerance).
- **Bug closures:** PR #39 stocks-mis-routing bug closed via server-side `asset_class` derivation. 2-line queue-routing bug fixed (`enqueue_ingest` now passes `_queue_name`; `IngestWorkerSettings.functions` includes `run_ingest`). Two latent bugs surfaced + fixed during live SPY demo: venue-convention mismatch (`SPY.XNAS` vs `SPY.NASDAQ`) → use `ensure_catalog_data` for canonical IDs; coverage check too strict → 7-day tolerance. Codex review P1 + P2 addressed post-PR (run_auto_heal error-containment + frontend loading state on poll errors).
- **Quality trail:** Plan review 8 iters (10→7→2→1→1→1→1→0), code review 3 iters (16→1→0), simplify 12 fixes, verify-app 6/6 gates GREEN (1896 pytest pass), E2E 5/5 UCs (UC-BAI-001 happy path demonstrated end-to-end with real SPY Jan 2024: 418 trades, Sharpe 4.97, +112.15% return in 12s wall-clock).
- **Follow-up deferred (see `## Next` items 7-8):** (a) Extend `/results` with equity_curve / drawdown_series / monthly_returns timeseries + wire trade log through UI (pre-existing empty charts — not a regression). (b) Bootstrap instrument registry from Databento catalog (avoid manual SQL seed for equities).

## Done (cont'd 10) — Dev stack restarted from new root + volume-name pinning (2026-04-20)

- **Problem:** post-flatten `docker compose` from the repo root created a NEW project (`msai-v2`) whose volumes got prefixed with that project name. The actual drill data lived in `live-path-wiring-registry_postgres_data` (from the worktree's compose invocation during PR #37 work). Bringing up the new project without a name-pin would silently spawn a fresh-empty Postgres and lose all registry rows, drill trades, and deployment history.
- **Fix 1 — data preserved:** Migrated `live-path-wiring-registry_postgres_data` → `msai_postgres_data` via a migration container (`alpine cp -a`). Verified 5 registry rows + 8 live trade fills + 7 deployments preserved.
- **Fix 2 — never again:** Pinned volume names explicitly in compose so any future project-name change (directory rename, worktree spawn, etc.) cannot orphan stateful volumes:
  - `docker-compose.dev.yml` — `postgres_data: { name: msai_postgres_data }`
  - `docker-compose.prod.yml` — same pin on `postgres_data`, `app_data`, `ib_gateway_settings` (3 prod-stateful volumes)
- **Root cause documented in compose comment:** prevents the next engineer from undoing the pin without understanding why it's there.
- **Orphan inventory** (left in place; destructive cleanup is a separate decision): `bug-bash_postgres_data`, `codex-version_postgres_data`, `claude-version_postgres_data`, `live-path-wiring-registry_postgres_data`, `mcpgateway_postgres_data`, `claude-version_ib_gateway_settings` — all pre-flatten or worktree artifacts, now superseded by the single pinned `msai_postgres_data`.

## Now

- **No active workflow.** Last shipped: **PR #45 "Symbol Onboarding: API + CLI + arq orchestrator"** squash-merged to main at `3bd22bd` on 2026-04-25. See "Done (cont'd 16)" below.
- **Stack:** main on `3bd22bd`. Local branch + worktree cleanup done. Origin branch deleted. Dev compose stack: `docker compose -f docker-compose.dev.yml up -d && ./scripts/restart-workers.sh` from the repo root when needed (worker container restart required to pick up the new `run_symbol_onboarding` arq task + `ingest_symbols` helper).
- **Next (post-#45 ratified backlog):**
  1. **#3 `instrument_cache` → registry migration** + `canonical_instrument_id()` removal.
  2. **Remaining CI-hardening backlog** — dependabot, pytest-xdist, coverage floor, compose smoke, security scans.
  3. **UI surface for Symbol Onboarding** (deferred per PR #45 PRD non-goal #1) — `/universe` page consuming the now-shipped `/api/v1/symbols/*` endpoints. Separate PRD.
- **Uncommitted on main (unrelated):**
  - `frontend/playwright.config.ts` baseURL reverted `:3300` → `:3000` — leftover from the 2026-04-22 computer reset. Revert when convenient.
  - `backend/tests/unit/observability/test_onboarding_metrics.py` — small dedicated unit test for the 3 onboarding metrics shipped in PR #45 (currently exercised only by the integration suite). Stage + commit as a follow-up if desired.

## Known issues surfaced this session (for follow-up — "no bugs left behind" tracker)

- **FE-01** Frontend Docker dev container can't resolve CSS imports (`tw-animate-css`, `shadcn/tailwind.css`) despite modules present. Pre-existing since PR #36; container-only. Host builds are fine.
- **BE-01** `msai instruments refresh --provider databento --symbols ES.n.0` hangs / errors with `FuturesContract.to_dict() takes no arguments (1 given)` at `parser.py:188` per verify-e2e agent. Local reproduction of the same function on synthetic FuturesContract succeeds — so error path is Databento-specific. Requires deeper trace.
- **IB-REGISTRY-01** `msai instruments refresh --provider interactive_brokers` requires IB Gateway container + matching paper/live port + account-prefix. Compose profile `broker` not active by default. Manual registry inserts (SQL) were used during the 2026-04-21 SPY live demo to bypass. Consider: (a) starting IB Gateway automatically on dev stack for sessions that need registry refresh, or (b) Databento-based equity registry refresh path that doesn't require IB.

## Next — remaining deferred items

### High-priority

1. ~~**CI hardening** — parse bug + ruff + pytest unblock~~ — **SHIPPED** as PR #42 (merged `8537ae2` 2026-04-23). See "Done (cont'd 13)". Remaining CI-hardening sub-items (moved to their own numbered backlog below in item 9).

2. **CI follow-up backlog** (post-PR-42):
   1. ~~**Mypy `--strict` cleanup**~~ — **SHIPPED** as PR #43 (merged `1fe65ea` 2026-04-23). 128 → 0 errors; mypy step is now a blocking CI gate. `actionlint` pre-commit hook also shipped.
   2. `.github/dependabot.yml` — prevents the kind of action-pin rot that made the `setup-uv@v4.3.0` bug in PR #36 hard to notice.
   3. `pytest-xdist -n auto` — free ~3x backend-test speedup.
   4. `--cov-fail-under=<baseline>` coverage floor.
   5. Optional docker-compose smoke test (`docker compose config --quiet` at minimum).
   6. Security scanning — `pip-audit`, `npm audit`, Trivy on Dockerfiles.

### From PR #32 ("db-backed-strategy-registry") + PR #35 scope-outs

2. ~~**Symbol Onboarding UI/API/CLI**~~ — **API + CLI SHIPPED** as PR #45 (merged `3bd22bd` 2026-04-25). UI surface deferred per PRD non-goal #1 (separate PRD when prioritized). See "Done (cont'd 16)".
3. **`instrument_cache` → registry migration.** Legacy `instrument_cache` table coexists with the new registry, not migrated yet. Skeleton at `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"InstrumentCache → Registry Migration".
4. ~~**Strategy config-schema extraction**~~ — **SHIPPED** as PR #38 (merged `663004c` 2026-04-20). Backend exposes `config_schema` + `config_defaults` via `/api/v1/strategies/{id}`; frontend auto-generates typed backtest forms.
5. **Remove `canonical_instrument_id()`** — Pablo override (2026-04-20): skip the council-suggested "one clean paper week" wait and schedule alongside items 3+4. Non-goal of PR #37 but ready to delete once verified no live deploys hit the legacy path.

### From PR #36 postscript

6. **Architecture-governance review (2026-10-19, 6-month cadence)** — revisit the Contrarian's minority report in `docs/decisions/which-version-to-keep.md`: (a) does the multi-login gateway fabric earn its complexity against actual multi-account operational load? (b) is the instrument registry + alias windowing justified by live-path usage or still scope creep?

### From PR #40 ("backtest-auto-ingest-on-missing-data") scope-outs — Pablo live-demo flags 2026-04-21

7. ~~**Backtest results UI: real charts + trade log**~~ — **SHIPPED** as PR #41 (merged `330e56a` 2026-04-23). `/results` now returns `series` (daily equity/drawdown + monthly returns); paginated `/trades` wired through to `<TradeLog>`; in-app QuantStats iframe via signed-URL flow. See "Done (cont'd 12)".

8. ~~**Instrument-registry seed from Databento catalog**~~ — **SHIPPED** as PR #44 (merged `b71aad3` 2026-04-24). `POST /api/v1/instruments/bootstrap` + `msai instruments bootstrap` removes the IB-Gateway-required step for equity registration. PR #45 then layered the operator-facing `/api/v1/symbols/onboard` watchlist surface on top. See "Done (cont'd 15)" + "Done (cont'd 16)".

### PR #35 documented known limitations

- **Midnight-CT roll-day race** — preflight and `_run_ib_resolve_for_live` call `exchange_local_today()` independently; narrow window, operator-recoverable.
- **CLI preflight doesn't accept registry-moved aliases for non-futures** — manifests only if IB qualification returned a venue the hardcoded `canonical_instrument_id` mapping doesn't match.
