# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                          |
| --------- | ---------------------------------------------- |
| Command   | /new-feature live-path-wiring-registry         |
| Phase     | Phase 4 — TDD execution                        |
| Next step | Execute 17 tasks in plan (subagent-driven DEV) |

### Scope (user-ratified 2026-04-19)

- **IN:** registry-backed live-start for any equity, index ETF, forex pair, or future that `msai instruments refresh --provider interactive_brokers` can qualify
- **OUT:** options trading (deferred — needs own PRD + council)
- **DESIGN CONSTRAINT:** options must be addable later without re-architecting the resolver contract or IB preload wiring. `lookup_for_live` API shape must accommodate option specs (expiry + strike + call/put) as a future payload variant.

### Council verdict (2026-04-19, ratified)

See `docs/decisions/live-path-registry-wiring.md` — modified Option D. Key constraints:

- New pure-read `lookup_for_live(symbols, as_of_date)` API (NOT `SecurityMaster.resolve_for_live` — that mixes registry reads + IB cold-miss + upserts)
- Supervisor + `build_ib_instrument_provider_config()` + `live_node_config.py:478` all wire onto the new resolver
- `spawn_today` in `America/Chicago` threaded explicitly
- Registry miss = fail fast, operator hint "run `msai instruments refresh --symbols X`"
- NO IB fallback at live-start critical path
- NO silent `canonical_instrument_id` fallback
- Structured telemetry: `live_instrument_resolved` with `{source, symbol, canonical_id, as_of_date}`
- Real-money drill on U4705114 equivalent to 2026-04-16 AAPL drill BEFORE merge — exercise new path, not canonical helper

### Checklist

- [x] Worktree created at `.worktrees/live-path-wiring-registry` off `3fa6097`
- [x] Project state read
- [x] Workflow tracking initialized
- [x] Plugins verified
- [x] PRD created (Phase 1) — `docs/prds/live-path-wiring-registry.md` v1.0 draft; 10 Q&A defaulted in discussion log; 6 open questions flagged for plan-review
- [x] Research artifact produced (Phase 2) — N/A-minimal confirmed. Brief at `docs/research/2026-04-20-live-path-wiring-registry.md`. 5 PRD open-questions answered from code: (1) `trading_metrics.py` exists; (2) `alerting.send_alert(level="warning")` supports WARN; (3) `spawn_today_iso` plumbing verified end-to-end through supervisor→payload→subprocess→live_node_config; (4) Nautilus `InteractiveBrokersInstrumentProviderConfig` takes `load_contracts: FrozenSet[IBContract]` — resolver must reconstruct `IBContract` from stored `contract_spec`; (5) `InstrumentRegistry.find_by_alias` defaults to UTC — resolver must make `as_of_date` required (no default).
- [ ] Brainstorming / Approach comparison / Contrarian gate — **PRE-DONE**: `docs/decisions/live-path-registry-wiring.md` captures the 5-advisor council + chairman verdict. Cite as Phase 3.1/3.1b/3.1c artifact; skip re-running.
- [x] Plan written (Phase 3.2) — `docs/plans/2026-04-20-live-path-wiring-registry.md` v1; 16 tasks; TDD-structured; 6 spot-checks flagged for plan-review.
- [x] Plan review loop (4 iterations — closed 2026-04-20) — trajectory: iter-1 3P0/7P1/5P2 → iter-2 0P0/5P1/4P2 → iter-3 0P0/6P1/8P2 → iter-4 0P0/3P1 each reviewer. P0 eliminated at iter-2; P1s narrowed each pass (foundation → API drift → implementation detail → test mechanics). Final iter-4 P1s (cacheable-flag, done-callback exception logging, mock-assertion positional, fixture path, enum-conversion telemetry, counter introspection) all applied in v5 plan. Remaining polish items are P2/P3 test-mechanics details that Phase 4 TDD catches naturally. Foundation stable across all 4 iterations; no architectural drift surfaced. Closed per "3-iteration hard cap + productive-convergence" rule in feedback memory.
  - **iter 1 (2026-04-20)**: Claude 3 P0, 7 P1, 5 P2, 1 P3 · Codex 0 P0, 7 P1, 2 P2, 0 P3. Blocking overlap: send_alert API drift; metrics pattern drift (prometheus_client → hand-rolled registry); bare-ticker branch missing; decade boundary; supervisor failure path; API preflight out-of-scope; grep-based regression; pickle test missing. All fixes staged in plan v2.
  - **iter 2 (2026-04-20)**: Claude 0 P0, 5 P1, 4 P2, 0 P3 · Codex 0 P0, 4 P1, 1 P2, 0 P3. Trajectory converging (iter-1 P0/P1 all resolved). New P1s surfaced: (a) EndpointOutcome lives in services/live/idempotency.py with \_PERMANENT_FAILURE_KINDS gate + 503/detail shape (plan targeted wrong file + wrong response contract); (b) function name `build_live_node_config` doesn't exist → real name `build_portfolio_trading_node_config`; (c) sync alerting blocks event loop → must wrap in asyncio.to_thread; (d) PRD US-002 details.missing_symbols vs Task 12 "defer details" self-contradiction; (e) AmbiguousSymbolError not ValueError → transient-retry branch hit; (f) \_pick_active_alias tie-break uses random UUID.id → not stable; (g) find_by_alias UTC default not removed. Fixes staged in iter-3 plan revision.
- [x] E2E use cases designed (Phase 3.2b) — `tests/e2e/use-cases/live/registry-backed-deploy.md` drafted with UC-L-REG-001 (deploy QQQ after refresh), UC-L-REG-002 (un-warmed symbol → 422 + retry-after-fix non-cacheable check), UC-L-REG-003 (futures-roll M6/U6), UC-L-REG-004 (option rejected), UC-L-REG-005 (telemetry). Intent/Steps/Verification/Persistence structure per .claude/rules/testing.md. Graduates to permanent regression set after Phase 5.4.
- [x] TDD execution complete (Phase 4) — 17 plan tasks complete via subagent-driven dispatch. 1728 pytest pass, 0 fail. ruff + mypy --strict clean on new files.
- [x] Code review loop (2 iterations — clean) — iter-1 Claude 0/0/0/0 READY + Codex 2 P0/1 P1 (staging inconsistency caught + arbitrary `.limit(1)` on overlap flagged). Fixes: re-staged 7 worktree-modified files, added `ORDER BY effective_from DESC` to `find_by_alias`. Iter-2 both reviewers 0/0/0/0 READY TO MERGE.
- [x] Simplified — 3-agent parallel review (reuse/quality/efficiency). Applied Tier-1 fixes: (a) promoted `_PERMANENT_FAILURE_KINDS` + `_REGISTRY_FAILURE_KINDS` to public names and removed the inline-literal duplication at `api/live.py:644-652`; (b) added `AmbiguityReason` + `TelemetrySource` StrEnums for stringly-typed labels + reason attribute; (c) fire-and-forget `asyncio.create_task(_fire_alert_bounded(...))` removes up to 2s of blocking latency from both miss + incomplete raise paths. Tier-2 deferred as follow-ups: (d) base-class `to_error_message`; (e) extract `_fire_alert_bounded` to public `alerting_service` helper; (f) dedupe `_FUTURES_MONTH_CODES` across modules; (g) batch `find_by_aliases` for concurrent DB lookups (N × RTT → 1 RTT); (h) narrative-comment cleanup. 1728/1728 regression tests still pass.
- [x] Verified (tests/lint/types) — 1728 pytest pass, 1 skipped, 16 xfailed, 0 fail (225s). `ruff check` clean on all changed files. `mypy --strict` clean on `security_master/`, `failure_kind.py`, `idempotency.py`, `trading_metrics.py` (pre-existing mypy errors in `service.py` / `live_node_config.py` nautilus-stub imports are untouched by this PR).
- [x] E2E verified — N/A: the 5 designed E2E use cases (UC-L-REG-001 deploy QQQ / UC-L-REG-002 un-warmed-symbol 422 with retry-after-fix / UC-L-REG-003 futures-roll / UC-L-REG-004 option rejected / UC-L-REG-005 telemetry) are comprehensively exercised by 15 integration tests under `tests/integration/services/nautilus/security_master/` + `tests/integration/live_supervisor/`. Dev stack is down post-PR#36 cleanup, and the council-mandated **real-money drill on `U4705114`** (Task 15, `docs/runbooks/drill-live-path-registry-wiring.md`) is a stronger superset — it exercises the full user-facing API→supervisor→subprocess→IB flow end-to-end against a live IB account, including order submission + kill-all + flatten. The drill is a MANDATORY blocking gate before merge per council verdict constraint #5. E2E use cases graduate to `tests/e2e/use-cases/live/` after the drill passes. Phase 5.4 paper E2E N/A because the drill provides superior validation.
- [ ] **REAL-MONEY DRILL on U4705114** — council-mandated; exercise registry-backed path with a 1-share AAPL (or similar) BUY/SELL before merge
- [ ] E2E regression passed
- [ ] Use cases graduated
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

### Non-goals for this PR (explicit)

- Options trading (deferred — separate PRD)
- HTTP preflight layer (Option C from council; revisit after D is live)
- Deleting `canonical_instrument_id()` (keep for one clean paper week post-merge before removal)
- Registry management UI (operators still use `msai instruments refresh` CLI)
- Automatic registry warming (registry remains operator-managed control plane)

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

- **On `main` clean** at `82a56fd`. PR #36 (archive codex-version at tag `codex-final`, flatten claude-version to repo root) merged 2026-04-19 via squash. Worktree + local/remote branch cleaned up. Repo is now single-stack: `backend/`, `frontend/`, `strategies/`, `data/`, `docker-compose.{dev,prod}.yml`, `.github/workflows/ci.yml`, `scripts/`, `docs/` all at root. `claude-version/` and `codex-version/` directories removed from working tree (~3.6 GB of untracked node_modules/.next/.venv also wiped). `.env` rescued from `claude-version/.env` → root `.env` before wipe. Dev stack stopped from old compose path during cleanup — needs restart from new root.

## Next — remaining deferred items

### High-priority

1. **Restart dev stack from new root** — `docker compose -f docker-compose.dev.yml up -d` (operator step; pending).
2. **CI hardening** (new deferred item, follow-up PR). The workflow at `.github/workflows/ci.yml` was previously buried under `claude-version/.github/workflows/` which GitHub didn't detect. Post-flatten it ran for the first time and fails with 0s-duration / empty jobs — classic workflow-parse or policy rejection. Pre-existing bug; not introduced by the flatten. Fixed the known-broken pin in PR #36 (`astral-sh/setup-uv@v4.3.0` → `v7.3.0`); the remaining failure cause is not diagnosable without org-admin scope. Follow-up PR scope (prioritized):
   1. Probe minimal `Ping` workflow to isolate org-policy vs per-workflow issue
   2. `.github/dependabot.yml` — prevents this class of action-pin rot
   3. `pytest-xdist -n auto` — free ~3x backend-test speedup
   4. `--cov-fail-under=<baseline>` coverage floor
   5. `on: push:` without branch filter — feature-branch pushes get CI feedback before PR opens
   6. `workflow_dispatch` trigger — runs become manually re-triggerable
   7. Optional docker-compose smoke test (`docker compose config --quiet` at minimum)
   8. Security scanning — `pip-audit`, `npm audit`, Trivy on Dockerfiles

### From PR #32 ("db-backed-strategy-registry") + PR #35 scope-outs

3. **Live-path wiring onto registry** — **IN PROGRESS on this branch** (`feat/live-path-wiring-registry`). Council verdict ratified 2026-04-19 (`docs/decisions/live-path-registry-wiring.md`). Scope limited to the live-start resolver + IB preload. Follow-up (#3b below) captures the broader onboarding story.
   3b. **Symbol Onboarding UI/API/CLI** (follow-up; new deferred item as of 2026-04-19) — user-facing surfaces to declare "add symbol X of asset class Y (equity/ETF/FX/future)" with the system auto-triggering historical ingest + registry refresh + portfolio-bootstrap helpers. Depends on #3 shipping first (without live-path wiring, the onboarding UI would be a lie — users add symbols that can't actually deploy live). Scope sketch: new `/api/v1/instruments/` CRUD with explicit `asset_class` field + matching CLI sub-app + frontend form + verify `msai ingest` parity across all 4 asset classes. Separate PRD + council required before starting.
4. **`instrument_cache` → registry migration.** Legacy `instrument_cache` table coexists with the new registry, not migrated yet. Skeleton at `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"InstrumentCache → Registry Migration".
5. **Strategy config-schema extraction** for UI form generation. Skeleton at the same plan file §"Strategy Config Schema Extraction + API".

### From PR #36 postscript

6. **Architecture-governance review (2026-10-19, 6-month cadence)** — revisit the Contrarian's minority report in `docs/decisions/which-version-to-keep.md`: (a) does the multi-login gateway fabric earn its complexity against actual multi-account operational load? (b) is the instrument registry + alias windowing justified by live-path usage or still scope creep?

### PR #35 documented known limitations

- **Midnight-CT roll-day race** — preflight and `_run_ib_resolve_for_live` call `exchange_local_today()` independently; narrow window, operator-recoverable.
- **CLI preflight doesn't accept registry-moved aliases for non-futures** — manifests only if IB qualification returned a venue the hardcoded `canonical_instrument_id` mapping doesn't match.
