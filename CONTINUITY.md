# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                           |
| --------- | ----------------------------------------------- |
| Command   | /new-feature backtest-results-charts-and-trades |
| Phase     | 4 — Execute (TDD)                               |
| Next step | superpowers:executing-plans (Phase 4)           |

### Checklist

- [x] Worktree created at `.worktrees/backtest-results-charts-and-trades` off `cc7d213` (main @ docs sync)
- [x] Project state read
- [x] Plugins verified — session skill inventory exposes `superpowers:*` + `pr-review-toolkit:*` + `research-first` agent; no Unknown-skill risk.
- [x] PRD discuss complete — `docs/prds/backtest-results-charts-and-trades-discussion.md` closed 2026-04-21. 6 refined user stories (US-001..US-006), 8 key decisions, 7 non-goals, 3 open questions routed to Phase 2/4.
- [x] PRD created — `docs/prds/backtest-results-charts-and-trades.md` v1.0. 6 user stories (US-001..US-006: populated charts, in-app full-report iframe, downloadable HTML, paginated trade log, graceful legacy-backtest rendering, distinct compute-failure state). 8 explicit non-goals. 5 success metrics (coverage, JSONB size <1MB, /results p99 <200ms, chart render <500ms, legacy no-500). 6 open questions routed to Phase 2 research + Phase 4 TDD (QS version stability, iframe render of 3-5MB HTML, 5yr minute-bar series-build cost, page_size overrun behavior, Next.js route handler pattern, re-materialize endpoint deferral).
- [x] Research artifact produced — `docs/research/2026-04-21-backtest-results-charts-and-trades.md` (63KB, 621 lines, 10 libraries deep-researched, 5 noted). 5 design-changing findings: (1) iframe proxy must stream via `new Response(upstream.body)` pattern A + `runtime='nodejs'` + `dynamic='force-dynamic'`; (2) **iframe auth requires server-side-only `MSAI_API_KEY` env var on frontend container** (`NEXT_PUBLIC_*` leaks to browser bundle — Entra JWT isn't accessible server-side today); requires docker-compose env-var addition in plan scope; (3) `ALTER TABLE ADD COLUMN series JSONB NULL` + `series_status VARCHAR(32) NOT NULL DEFAULT 'not_materialized'` are metadata-only on Postgres 16 — no table rewrite; (4) drawdown formula reference: `equity / running_max - 1` (existing analytics_math.py is correct — forward-looking plan guidance); (5) monthly heatmap: CSS Grid + Tailwind oklch (~60 LOC), NOT Recharts (no heatmap primitive). 10 open risks flagged (iframe auth mechanism, QS HTML paint perf unmeasured, payload-size leak risk on minute-bar, divergence UI flag, legacy report file cleanup, page_size 422-vs-clamp convention, React 19 hydration edge cases, Recharts v3 deprecation watch, Next 16 forward-compat, docker-compose env propagation runbook).
- [x] Design guidance loaded — `/ui-design` skill, **Mode: Product UI**. Rationale: authenticated dashboard surface for a single power user; dense data display (6 metric cards + 4 chart components + paginated trade log); not marketing (no wow-factor); not Trust-First (no irreversible actions). Key decisions: `<Tabs>` wrapper (Native view / Full report iframe), functional-motion-only (150–200ms transitions), skeleton loaders during poll, color+icon+text for `series_status` (ready → none, not_materialized → gray info, failed → amber warning), new monthly heatmap as CSS Grid + Tailwind oklch (~60 LOC per research finding #5), reuse existing shadcn primitives + Recharts, skip 21st.dev lookup (primitives sufficient; net-new components are data-shape-specific). Full notes get embedded in the plan file at Phase 3.2.
- [x] Brainstorming / Approach comparison / Contrarian gate — **PRE-DONE** via standalone `/council` 2026-04-21. 5 advisors (Simplifier/Scalability Hawk/Pragmatist via Claude + Contrarian/Maintainer via Codex) + Codex xhigh chairman. Verdict: `A.3 + B.1 + C.2` (hybrid render + canonical `Backtest.series` JSONB + 4 native components + authenticated iframe proxy). Ratified decision doc at `docs/decisions/backtest-results-charts-and-trades.md`. Minority Report (Contrarian OBJECT) preserved: iframe auth proxy mandatory; canonical daily-normalized series (not pre-digested blobs) mandatory; A.2 native-only overruled but re-openable if iframe UX hurts. Per feedback memory `skip_phase3_brainstorm_when_council_predone`, skip Phase 3.1/3.1b/3.1c re-runs.
- [x] Council verdict: A.3 + B.1 + C.2 with must-do constraints (iframe proxy, canonical series, atomic write, `series` naming, single normalization path, `series_status` flag, paginated `/trades`, payload observability).
- [x] Plan written — `docs/plans/2026-04-21-backtest-results-charts-and-trades.md`. 17 tasks (B1–B10 backend + F1–F7 frontend) + 6 E2E use cases (UC-BRC-001..006). Backend: Alembic migration for `series` JSONB + `series_status` VARCHAR(32), `SeriesPayload`/`SeriesStatus` Pydantic schemas, dedupe `normalize_daily_returns` as canonical returns helper, `build_series_payload()` with daily equity/drawdown + monthly aggregation, atomic worker-side materialization in `_finalize_backtest` (fail-soft sets `series_status="failed"`), `/results` extended + trades dropped inline, NEW paginated `GET /trades?page=N&page_size=100` (clamp 500 max), payload-size observability histograms, docker-compose `MSAI_API_KEY` env var. Frontend: TS types (`SeriesPayload`, `SeriesStatus`, fixed `BacktestTradeItem`), Next.js 15 Route Handler proxy at `/api/backtests/[id]/report/route.ts` (Pattern A streaming via `new Response(upstream.body)` + `runtime='nodejs'` + `dynamic='force-dynamic'`), Tabs wrapper (Native view / Full report iframe), wired equity + drawdown (Recharts AreaChart), native MonthlyReturnsHeatmap (CSS Grid + Tailwind oklch ~60 LOC), paginated TradeLog, SeriesStatusIndicator shared empty-state component.
- [x] Plan review loop (11 iterations — APPROVED 2026-04-21). Trajectory: 12→8→4→3→3→1→3→2→1→3→0/0/0 (productive convergence per feedback memory). Foundation stable from iter-2 onward. Architectural pivot in iter-9: iframe auth bypass caught + redesigned as signed-URL flow (HMAC signer + `POST /report-token` + `GET /report?token=` extension + origin-qualified iframe src) — dropped the unsafe Next.js Route Handler with server-side `MSAI_API_KEY`. Final iter-11 verdict APPROVED with 2 P3 wording nits (non-blocking; one fixed, one left as obsolete-marker for forensic reference). Historical iter-7 (pre-iter-8 placeholder): (7 iterations — iter-7 NEEDS*REVISION, 0 P0 + 0 P1 + 2 P2 + 1 P3, fixes applied; iter-8 pending) — trajectory 12→8→4→3→3→1→3 (slight up-tick in iter-7 from new observability-contract nits not spotted in prior iters — normal for exhaustive review; foundation stable). Iter-7 fixes: (1) PRD+plan aligned on UNPREFIXED log event names matching project structlog convention (`msai*`prefix stays on Prometheus metric names only); (2)`payload_bytes`+`nautilus_version`fields added to the PRD-contract logs via new`nautilus_version`param on`\_materialize_series_payload`; (3) `msai_backtest_trades_page_count` counter added to B9 + incremented in /trades handler (matches PRD §7); (4) mock-handler bug fixed (`scalar_one_or_none() is None`branch now triggered); (5) P3`report_path`leak → schema uses`has_report: bool`derived server-side, TS type + iframe tab updated. — trajectory 12→8→4→3→3→1, near-zero productive convergence. Iter-6 fix: success-path test also asserts`log_level=="info"` on captured structlog entry (matches precedent). — iter-5 fixes: (P1) full-models-registry import (`import msai.models`) ensures User/etc tables register before `Base.metadata.create_all()`; (P1) Strategy seed adds required `file_path`+`strategy_class`non-null fields; (P1) log assertions switched from`caplog`to`structlog.testing.capture_logs()`matching existing test pattern at`test_auto_heal.py:251`; (P2) `isolated_session_maker`adds`drop_all`before`create_all`to prevent cross-test row leakage within the module-scoped container. — trajectory 12→8→4→3, tight convergence. Iter-4 fixes: (P1) defined per-module`isolated_postgres_url`fixture directly inside`test_backtest_job_finalize.py`(not shared repo-wide); (P1) added`\_seed_backtest_with_strategy_parent()`helper to seed Strategy FK parent before Backtest (previous code would FK-violate on commit); (P2) replaced`pytest.skip(...)` failure-path test with two concrete executable tests (`test_materialize_series_payload_success_returns_ready`+`test_materialize_series_payload_failure_returns_failed`) targeting a NEW `\_materialize_series_payload`helper now mandated as Change 3a in B5 implementation (was optional Case A). — iter-1 trajectory: 4 P0 + 5 P1 + 3 P2 + 0 P3. Both reviewers (Claude + Codex) converged on: (P0)`\_finalize_backtest`signature wrong, Histogram API absent,`useAuth`import wrong,`report_path`not in schema. (P1) missed reuse of`build_series_from_returns`, migration test path wrong, line anchors stale, test fixtures missing, `normalize_daily_returns`regresses existing behavior, UC-BRC-004 wrong endpoint path. (P2) observability metric name drift, trade-pagination non-determinism on equal executed_at, UC-BRC-002 ARRANGE wording. Iter-2 Codex: 1 P0 + 4 P1 + 3 P2 (narrower — trajectory 12 → 8). Iter-2 fixes applied: B9`\_registry.histogram`→`\_r.histogram`to match`trading_metrics.py:16-18`pattern; B0 rewritten to dual-pattern (pure factories in`unit/conftest.py`+ per-module`session_factory`in`test_backtest_job_finalize.py`, matching `test_backtest_live_parity.py:42-60`; NO shared `integration/conftest.py`which doesn't exist); B0b Histogram uses`self.name`/`self.help_text`(not`self.\_name`) + returns `list[str]`(not`str`) + `metric_type="histogram"`; B9 tests use `registry.render()`text assertions (not`\_count`/`\_buckets`internals);`\_make_backtest_with_trades(n)`is sync (fixes "coroutine not tuple" bug); B5 caller-failure test fleshed out with monkeypatch + capture pattern (was`pass`); invalid `from msai.services.nautilus.backtest_runner import (...)`empty import removed; all stale`tests/unit/test_backtest_job.py`/`test_backtest_metrics.py`/`test_trading_metrics.py`paths redirected to existing files; UC-BRC-004 step 4 endpoint path fixed; added test-pattern note before B6 pointing implementers to`\_mock_session_returning(row)`+`get_db`override + shared`client`. All 12 findings addressed in plan v2 with: new Task B0 (persistence fixtures), new Task B0b (Histogram primitive), B3 preserves existing `\_normalize_report_returns`semantics verbatim, B4 delegates to`build_series_from_returns`, B5 moves series-materialization to caller + keeps `\_finalize_backtest` signature +2 keyword-only params (`series_payload`+`series_status`), B6 adds `report_path: str | None`to BacktestResultsResponse, B7 line anchors fixed (trade serialization 426-447 not 428-447), B8 secondary sort`Trade.id.asc()`, B9 single canonical `msai_backtest_results_payload_bytes`histogram (dropped invented names), F1`report_path`in TS type, F3`results?.report_path`, F6 `useAuth`from`@/lib/auth`+ async`getToken()`in-effect, UC-BRC-004 endpoint path`/api/v1/backtests/{id}/trades`, UC-BRC-002 rewritten to use migration-default state.
- [ ] TDD execution complete
- [ ] Code review loop (0 iterations) — iterate until no P0/P1/P2
- [ ] Simplified
- [ ] Verified (tests/lint/types)
- [ ] E2E use cases designed (Phase 3.2b)
- [ ] E2E verified via verify-e2e agent (Phase 5.4)
- [ ] E2E regression passed (Phase 5.4b)
- [ ] E2E use cases graduated to tests/e2e/use-cases/ (Phase 6.2b)
- [ ] E2E specs graduated to tests/e2e/specs/ (Phase 6.2c — if Playwright framework installed)
- [ ] Learnings documented (if any)
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

### Scope seed (to refine during PRD discuss)

**User ask (verbatim during PR #40 SPY live demo 2026-04-21):** backtest results UI renders empty Equity Curve / Drawdown / Monthly Returns Heatmap / Trade Log despite the backend computing all this via QuantStats. The downloadable `/report` HTML has everything; the React UI doesn't get piped the data.

**Scope seed** (from CONTINUITY.md Next #7, UI-RESULTS-01):

- **Backend** — extend `/api/v1/backtests/{id}/results` response with timeseries fields the detail page already has component slots for: `equity_curve` (date, equity, drawdown), `monthly_returns` (month, pct), `daily_returns` (date, pct). Either parse from the existing QuantStats DataFrame or re-derive from the Nautilus positions log. Data already exists — this is a wiring task.
- **Backend** — wire `results.trades` through from `/results` (backend already computes 400+ trade rows per backtest but frontend receives `[]`).
- **Frontend** — fix `BacktestTradeItem` TS type: backend sends individual fills (`id`, `instrument`, `side`, `quantity`, `price`, `pnl`, `commission`, `executed_at`); frontend's `<TradeLog>` expects entry/exit round-trips (`entryPrice`, `exitPrice`, `holdingPeriod`). Decide: pair fills into round-trips server-side OR update TS type + TradeLog UI to render individual fills.
- **Frontend** — remove the `equityCurve: []` hardcode at `frontend/src/app/backtests/[id]/page.tsx:203` and the accompanying "backend doesn't return X yet" placeholder comments. Wire the three chart components + TradeLog to real data.
- **Frontend** — Monthly Returns Heatmap already exists at `ResultsCharts.MonthlyReturnsHeatmap`; currently shows "Monthly returns data not yet available from the backend." — wire it to the new backend field.

**Out of scope (defer-again candidates):**

- Drawdown component visualization innovations beyond what Recharts offers (keep the existing chart component).
- Side-by-side backtest comparison UI.
- Export trade log to CSV (QuantStats HTML already downloads today).
- Changes to how QuantStats is invoked in the worker (its output is the ground-truth data source).

**Key open questions for PRD discuss:**

- Pair-fills-into-round-trips server-side vs render individual fills (affects TS type, UI component, and whether we're changing the semantic model).
- Parse QuantStats DataFrame directly (fast, single-source-of-truth, but couples us to QuantStats internals) vs re-derive from Nautilus positions log (more robust but duplicates calculation).
- Should `/results` stay a single endpoint returning everything, or split into `/results` (aggregates) + `/results/timeseries` (charts) + `/results/trades` (trade log) for pagination/caching?
- Payload size: a 1-year daily equity curve is ~250 points; a 1-year minute-bar equity curve could be 100K+ points. Do we downsample on the server?
- Future minute-bar support: if someone runs a minute-bar backtest over 5 years (~500K bars), the equity curve can't be a single JSON blob. Add daily downsample now or later?

### Legacy workflow archive (auto-ingest PR #40, failure-surfacing PR #39, strategy-config-schema PR #38, live-path-wiring PR #37)

Historical checklists + approach comparisons from merged PRs removed 2026-04-21 during `/new-feature backtest-results-charts-and-trades` worktree init. Canonical narrative lives in `## Done (cont'd N)` sections below and in `docs/CHANGELOG.md`. Use `git log -p CONTINUITY.md` if you need the full text.

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

- **No active workflow.** Last shipped: PR #40 "Backtest auto-ingest on missing data — transparent self-heal" (merged `43051da` 2026-04-21). See "Done (cont'd 11)" for details.
- **2026-04-21 session edit (uncommitted on `main`, no workflow):** synced CLAUDE.md to `claude-codex-forge/CLAUDE.template.md` — added (1) `@CONTINUITY.md` import at line 1 to auto-load CONTINUITY every session, (2) `### Research Enforcement` subsection explaining `research-first` Phase-2 output at `docs/research/YYYY-MM-DD-<feature>.md`, (3) top-level `## No Bugs Left Behind Policy` H2 surfacing the critical-rules.md policy directly in CLAUDE.md.
- **2026-04-21 Playwright scaffold location — RESOLVED (Option b, forge intent):** scaffold moved to `frontend/`. Forge's `setup.sh --with-playwright` auto-detects the lone `package.json` subdir (finds `frontend/` in msai-v2). Executed: deleted root `playwright.config.ts` + root `tests/e2e/{.auth,fixtures}/`; kept root `tests/e2e/{use-cases,reports}/` (agent artifacts, not part of scaffold); fixed `frontend/playwright.config.ts` baseURL `:3000` → `:3300` (host-exposed Docker port for local runs); accepted the uncommitted `docs/ci-templates/{README.md,e2e.yml}` diff (stamps `working-directory: frontend`); updated CLAUDE.md file-tree block + Playwright Framework section to match. Commit pending.
- **Follow-up candidates** (see `## Next — remaining deferred items`):
  - Item #7: extend `/backtests/{id}/results` with timeseries fields (equity_curve, drawdown_series, monthly_returns) + wire TradeLog. Pre-existing UI gap, user-flagged during SPY demo.
  - Item #8: Databento catalog bootstrap for instrument registry (avoid manual SQL seed for equities).
- **Stack:** main on `43051da`; worktrees clean (all merged); dev compose volumes preserved via pins (see "Done cont'd 10"). `docker compose -f docker-compose.dev.yml up -d` from `/Users/pablomarin/Code/msai-v2` if you want to keep the stack running. 3. **Pause cold.** All work durable on disk (15 files modified/new). Resume in a fresh session.

## Known issues surfaced this session (for follow-up — "no bugs left behind" tracker)

- **FE-01** Frontend Docker dev container can't resolve CSS imports (`tw-animate-css`, `shadcn/tailwind.css`) despite modules present. Pre-existing since PR #36; container-only. Host builds are fine.
- **BE-01** `msai instruments refresh --provider databento --symbols ES.n.0` hangs / errors with `FuturesContract.to_dict() takes no arguments (1 given)` at `parser.py:188` per verify-e2e agent. Local reproduction of the same function on synthetic FuturesContract succeeds — so error path is Databento-specific. Requires deeper trace.
- **UI-RESULTS-01** `/backtests/{id}/results` returns only 6 aggregate metrics — no timeseries (`equity_curve`, `drawdown_series`, `monthly_returns`). Detail page at `frontend/src/app/backtests/[id]/page.tsx:203` hardcodes `equityCurve: []` with comment `// The backend results endpoint does not yet return an equity curve or a trade log. Show empty charts until the backend supports it.` — UI renders empty `Equity Curve`, `Drawdown` chart, `Monthly Returns Heatmap`. **Pre-existing, NOT introduced by auto-ingest PR.** Pablo flagged 2026-04-21 after live SPY demo. Follow-up PR scope: extend `/results` response shape + wire the three charts + wire `<TradeLog trades={[]} />` (backend already returns 418+ trades but frontend doesn't pass them through; also requires TS type fix from individual-fill shape to entry/exit-pair shape). QuantStats HTML report download button works today; piping its data into React is the follow-up.
- **IB-REGISTRY-01** `msai instruments refresh --provider interactive_brokers` requires IB Gateway container + matching paper/live port + account-prefix. Compose profile `broker` not active by default. Manual registry inserts (SQL) were used during the 2026-04-21 SPY live demo to bypass. Consider: (a) starting IB Gateway automatically on dev stack for sessions that need registry refresh, or (b) Databento-based equity registry refresh path that doesn't require IB.

## Next — remaining deferred items

### High-priority

1. **CI hardening** (new deferred item, follow-up PR). The workflow at `.github/workflows/ci.yml` was previously buried under `claude-version/.github/workflows/` which GitHub didn't detect. Post-flatten it ran for the first time and fails with 0s-duration / empty jobs — classic workflow-parse or policy rejection. Pre-existing bug; not introduced by the flatten. Fixed the known-broken pin in PR #36 (`astral-sh/setup-uv@v4.3.0` → `v7.3.0`); the remaining failure cause is not diagnosable without org-admin scope. Follow-up PR scope (prioritized):
   1. Probe minimal `Ping` workflow to isolate org-policy vs per-workflow issue
   2. `.github/dependabot.yml` — prevents this class of action-pin rot
   3. `pytest-xdist -n auto` — free ~3x backend-test speedup
   4. `--cov-fail-under=<baseline>` coverage floor
   5. `on: push:` without branch filter — feature-branch pushes get CI feedback before PR opens
   6. `workflow_dispatch` trigger — runs become manually re-triggerable
   7. Optional docker-compose smoke test (`docker compose config --quiet` at minimum)
   8. Security scanning — `pip-audit`, `npm audit`, Trivy on Dockerfiles

### From PR #32 ("db-backed-strategy-registry") + PR #35 scope-outs

2. **Symbol Onboarding UI/API/CLI** — user-facing surfaces to declare "add symbol X of asset class Y (equity/ETF/FX/future)" with the system auto-triggering historical ingest + registry refresh + portfolio-bootstrap helpers. Now unblocked since PR #37 shipped the live-path wiring. Scope sketch: new `/api/v1/instruments/` CRUD with explicit `asset_class` field + matching CLI sub-app + frontend form + verify `msai ingest` parity across all 4 asset classes. Separate PRD + council required before starting.
3. **`instrument_cache` → registry migration.** Legacy `instrument_cache` table coexists with the new registry, not migrated yet. Skeleton at `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"InstrumentCache → Registry Migration".
4. **Strategy config-schema extraction** — **IN PROGRESS on this branch** (`feat/strategy-config-schema-extraction`). Phase 0 spike PASSED. Phases 1/2/3.2 artifacts landed. Next: plan review → Phase 4 TDD (B1–B7 backend + F1–F2 frontend). Plan at `docs/plans/2026-04-20-strategy-config-schema-extraction.md`.
5. **Remove `canonical_instrument_id()`** — Pablo override (2026-04-20): skip the council-suggested "one clean paper week" wait and schedule alongside items 3+4. Non-goal of PR #37 but ready to delete once verified no live deploys hit the legacy path.

### From PR #36 postscript

6. **Architecture-governance review (2026-10-19, 6-month cadence)** — revisit the Contrarian's minority report in `docs/decisions/which-version-to-keep.md`: (a) does the multi-login gateway fabric earn its complexity against actual multi-account operational load? (b) is the instrument registry + alias windowing justified by live-path usage or still scope creep?

### From PR #40 ("backtest-auto-ingest-on-missing-data") scope-outs — Pablo live-demo flags 2026-04-21

7. **Backtest results UI: real charts + trade log** (follow-up PR). Today `/api/v1/backtests/{id}/results` returns only 6 aggregate metrics. The detail page renders empty Equity Curve / Drawdown / Monthly Returns Heatmap / Trade Log components. QuantStats HTML report (full analytics, 60+ stats) IS generated per backtest and downloadable via the `Download Report` button, but its data isn't piped into the React UI. Scope sketch:
   1. Extend backend `BacktestResultsResponse` with `equity_curve: list[{date, equity, drawdown}]`, `monthly_returns: list[{month, pct}]`, `daily_returns: list[{date, pct}]` (or parse directly from the QuantStats output DataFrame — it's already computed).
   2. Wire `results.trades` through from `/results` to `<TradeLog trades={results.trades}>` on the detail page (currently hardcoded `[]`).
   3. Fix `BacktestTradeItem` TS type — backend sends individual fills (`id`, `instrument`, `side`, `quantity`, `price`, `pnl`, `commission`, `executed_at`); TS expects entry/exit round-trips (`entryPrice`, `exitPrice`, `holdingPeriod`, `timestamp`). Either pair fills into round-trips on the backend, or adapt the TS type + TradeLog UI to render individual fills.
   4. Drawdown chart uses `equity_curve[].drawdown` (derived, already in QuantStats).
   5. Monthly returns heatmap component already exists at `ResultsCharts.MonthlyReturnsHeatmap` — currently hardcoded "Monthly returns data not yet available from the backend." message. Once backend ships `monthly_returns`, wire it.

8. **Instrument-registry seed from Databento catalog** (follow-up PR). During PR #40 live demo, registry had only `ES.n.0.XCME` — stocks had to be inserted manually via SQL. A bootstrap script that queries Databento's `list_symbols` for XNAS.ITCH + GLBX.MDP3 + seeds the registry with the top-N most-liquid instruments would unblock cold-start deployments and remove the IB-Gateway-required step for equity registration.

### PR #35 documented known limitations

- **Midnight-CT roll-day race** — preflight and `_run_ib_resolve_for_live` call `exchange_local_today()` independently; narrow window, operator-recoverable.
- **CLI preflight doesn't accept registry-moved aliases for non-futures** — manifests only if IB qualification returned a venue the hardcoded `canonical_instrument_id` mapping doesn't match.
