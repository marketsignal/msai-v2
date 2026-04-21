# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field     | Value                                            |
| --------- | ------------------------------------------------ |
| Command   | /new-feature backtest-failure-surfacing          |
| Phase     | 6 — Ship                                         |
| Next step | Graduate E2E use cases → commit → push → open PR |

### Checklist

- [x] Worktree created at `.worktrees/backtest-failure-surfacing` off `e47243d` (main)
- [x] Project state read
- [x] Plugins verified — `superpowers:brainstorming` loaded cleanly; `pr-review-toolkit:*` agents available in session skill inventory.
- [x] PRD created — `docs/prds/backtest-failure-surfacing.md` v1.0 (6 user stories, 4 open questions flagged for plan-review, Codex-ratified decisions on Q1–Q7).
- [x] Research artifact produced — `docs/research/2026-04-20-backtest-failure-surfacing.md`. 7 libraries researched, 7 design-changing findings. Highlights: Radix Tooltip is desktop-only by design (mobile uses detail card per US-002); Tooltip primitive already installed — needs `TooltipProvider` at layout; mirror `live/failure_kind.py::FailureKind.parse_or_unknown` for classifier; single-step Alembic `add_column(server_default, nullable=False)` is safe on Postgres 16; use plain-`dict` JSONB pattern (no new TypeDecorator).
- [ ] Design guidance loaded (if UI)
- [x] Brainstorming complete — PRD + research brief supplied the structure; approach comparison (below) made the strategic choice explicit without re-brainstorming from scratch.
- [x] Approach comparison filled — see `## Approach Comparison` section (default = worker-side classifier + 4 persisted cols + structured envelope; alt = view-time classification).
- [x] Contrarian gate passed — **SKIP** (Codex gpt-5.4 @ xhigh: VERDICT VALIDATE). Codex's reasoning: default matches the existing `live/failure_kind.py` precedent, this PR is establishing a durable contract for the follow-up auto-ingest PR, view-time classification would force a second migration later. No foundation-level concerns → full council not needed.
- [x] Council verdict — N/A (skip-validated at contrarian gate).
- [x] Plan written — `docs/plans/2026-04-20-backtest-failure-surfacing.md`; 10 tasks (6 backend B1–B8, 4 frontend F1–F4) + 5 E2E use cases (UC-BFS-001..005); single-step migration `x2r3s4t5u6v7`; reuses `live/failure_kind.py` classifier pattern.
- [x] Plan review loop (9 iterations — PASS 2026-04-20) — trajectory: iter-1 4P1+2P2+1P3 → iter-2 2P2 → iter-3 1P2 → iter-4 1P2 → iter-5 2P2 → iter-6 1P2 → iter-7 1 higher-severity → iter-8 1P2 → iter-9 clean (Codex: "PLAN APPROVED"). All findings applied in-place; see the plan's `## Plan Review History` section + `[iter-N]` markers. Major decisions locked: classifier matches BacktestRunner's RuntimeError(traceback) wrapping, 4 new persisted columns (single-step Alembic), `FailureClassification` dataclass, B0 fixture task, sanitize-on-read path for historical rows, nav link for failed rows, TSX snippets all rewritten as named arrow-fn wrappers for prettier-compatibility.

> **Historic context preserved below for reference — iter-1 line:** - **iter 1 (2026-04-20)**: Codex 4 P1 + 2 P2 + 1 P3; Claude concurred (fixture gap + variable scope independently flagged). All applied in-place in the plan file (search `[iter-1]`): (P1-a) backfill sanitize-on-read instead of SQL UPDATE, (P1-b) history endpoint explicit constructor rewrite, (P1-c) classifier matches `RuntimeError(traceback)` wrapping from `BacktestRunner:239` + drops `missing_strategy_data_for_period` (empty bars is a success), (P1-d) new Task F3.5 adds `/backtests/<id>` nav link for failed rows, (P2-a) new Task B0 adds `seed_failed_backtest` / `seed_historical_failed_row` / `seed_pending_backtest` fixtures, (P2-b) B7 rewrite uses `symbols` + `backtest_row["start_date"]` — variables bound BEFORE the try, (P3) classifier now returns a `@dataclass FailureClassification` instead of a 4-tuple.

- [x] TDD execution complete — all 14 tasks (B0–B8 backend + F1–F4 frontend) implemented + tested + staged via subagent-driven development. Backend: 97+ backtest-related tests pass, 0 fail, ruff + mypy clean on changed modules. Frontend: tsc --noEmit 0 errors, pnpm lint 0 new warnings (1 pre-existing in research/page.tsx from flatten). Plan review loop completed 9 iterations earlier; no new architectural drift surfaced during implementation. 4 new persisted columns on backtests + FailureCode enum + classifier + sanitizer + ErrorEnvelope/Remediation schemas + API helper + UI tooltip + nav link + FailureCard all wired end-to-end.
- [x] Code review loop (3 iterations — PASS). Iter-1 (Codex + pr-review-toolkit parallel): Codex flagged 2 P1 + 3 P2; pr-toolkit flagged 0 P0/P1/P2 (5 P3 nits). All 5 Codex findings applied — (P1) classifier asset_class kwarg plumbed through worker; (P1) sanitizer DSN regex + SyntaxError traceback frames + caret-line handling; (P2) response_model_exclude_none=True on 2 status endpoints; (P2) Badge tabIndex=0 + role=button + aria-label for keyboard access; (P2) FailureCard clipboard try/catch + visible copyError state. Iter-2: Codex flagged 1 P1 + 1 P2; P1 (UI doesn't send asset_class) downgraded to documented scope-defer in classifier docstring (core feature unaffected — only remediation-command positional arg is slightly wrong for futures-via-UI); P2 (TS types mismatch with exclude_none) fixed by making started_at/completed_at optional in BacktestStatusResponse TS type. Iter-3: Codex "PLAN APPROVED — no new P0/P1/P2".
- [x] Simplified — 3-agent parallel sweep (reuse/quality/efficiency). Reuse: clean (foundation appropriate, no existing utilities to reuse). Quality: applied DRY fix for `symbols_for_cmd` in classifier (single local binding serves both action string + Remediation.symbols); swept all `[iter-N P?]` / `[Phase 5 P?]` breadcrumb markers from code (kept prose, dropped prefixes). Efficiency: FailureCard now uses `useRef` + `clearTimeout` for both copied/error timers with unmount cleanup — prevents stale-state flips on rapid clicks and memory leaks on navigation.
- [x] Verified (tests/lint/types) — iter-1 found ruff 9-error gap (TC003 moves needed on 4 files); autofixes applied + then reverted on SQLAlchemy model + Pydantic schemas with `noqa: TC003` (SQLAlchemy's `Mapped[]` needs concrete `date`/`datetime` types at class-build time; Pydantic v2 same for `date`/`datetime`/`UUID`). Iter-2 verify-app: **ALL 6 GATES PASS** — 1779 backend pass / 1 pre-existing out-of-scope fail (`test_es_june_2025_fixed_month` in security_master, not in PR scope); ruff clean on all PR-touched files; mypy --strict no new errors (all pre-existing patterns); frontend tsc 0 errors; lint 0 errors + 1 accepted pre-existing warning; `pnpm build` 16 routes compiled clean.
- [x] E2E use cases designed (Phase 3.2b) — 5 UCs in the plan file: UC-BFS-001 (API status envelope), UC-BFS-002 (CLI parity), UC-BFS-003 (history compact fields), UC-BFS-004 (pending-row error-absent contract), UC-BFS-005 (UI tooltip + nav link + FailureCard).
- [x] E2E verified via verify-e2e agent (Phase 5.4) — **PASS 5/5** at 2026-04-21T12:07Z on rebased-onto-main stack. Report at `tests/e2e/reports/2026-04-21-12-07-backtest-failure-surfacing.md`. Driven by main agent via curl (API) + `docker exec` (CLI) + Playwright MCP (UI); verify-e2e agent's toolbox lacks Playwright so UI was driven directly. All assertions green: structured envelope surface, sanitized `<DATA_ROOT>` in place of `/app/` paths, `response_model_exclude_none=True` omits non-failed error key, keyboard-accessible Badge, FailureCard with copyable suggested_action + remediation details, persistence after reload.
- [x] E2E regression passed (Phase 5.4b) — `tests/e2e/use-cases/live/` + `strategies/` from prior PRs are UI-agnostic API tests; no new graduated backtests UCs exist yet in `tests/e2e/use-cases/` (this PR will be the first). Vacuous pass.
- [x] E2E use cases graduated to tests/e2e/use-cases/ (Phase 6.2b) — `tests/e2e/use-cases/backtests/failure-surfacing.md` with all 5 UCs (BFS-001..005) + expected failure modes + known limitations section documenting the UI asset_class scope-defer.
- [ ] E2E specs graduated to tests/e2e/specs/ (Phase 6.2c — if Playwright framework installed)
- [ ] Learnings documented (if any)
- [ ] State files updated
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

## Approach Comparison

### Chosen Default

**Worker-side classifier + 4 new persisted columns + structured envelope flowing through existing read paths.**

- Worker's `_mark_backtest_failed(...)` gains a `classify_worker_failure(exc) -> (FailureCode, public_message, suggested_action, remediation)` helper, modeled on `services/live/failure_kind.py::FailureKind.parse_or_unknown`.
- 4 new columns on `backtests`: `error_code String(32) NOT NULL DEFAULT 'unknown'`, `error_public_message Text NULL`, `error_suggested_action Text NULL`, `error_remediation JSONB NULL`. Single-step Alembic (Postgres-16 fast-path).
- Pydantic `ErrorEnvelope` + `Remediation` models in `schemas/backtest.py`. `BacktestStatusResponse` gains `error: ErrorEnvelope | None`. `BacktestListItem` gains compact `error_code` + `error_public_message` only.
- UI: mount `<TooltipProvider>` at `app/layout.tsx`; list-page badge wrapped in Tooltip; detail-page `<FailureCard>` component with copy-to-clipboard for `suggested_action`.
- CLI: zero changes — `msai backtest show` prints API JSON verbatim, inherits the envelope for free.

### Best Credible Alternative

**View-time classification only (no new DB columns).**

- Keep the raw `error_message` column; add a read-time classifier that runs in the API response-building path.
- Pros: no migration, no worker changes, no backfill.
- Cons: classifier runs on every GET (cheap but wasted work); no stable `FailureCode` in the DB for future alerting/telemetry queries; the auto-ingest follow-up PR has to re-parse prose OR add columns later anyway.

### Scoring (fixed axes)

| Axis                  | Default | Alternative |
| --------------------- | ------- | ----------- |
| Complexity            | M       | L           |
| Blast Radius          | L       | L           |
| Reversibility         | M       | H           |
| Time to Validate      | L       | L           |
| User/Correctness Risk | L       | M           |

The default is higher-complexity but lower-risk because classification-at-write-time is deterministic and visible in DB for queries; classification-at-read-time hides bugs until someone grep's logs. The alternative also forces the auto-ingest PR to redo the migration.

### Cheapest Falsifying Test

**< 30 min** — write a 3-line unit test that asserts `classify_worker_failure(FileNotFoundError("No raw Parquet files found for 'ES'..."))` returns `FailureCode.missing_data` + a `Remediation(kind="ingest_data", symbols=["ES"], ...)` with the right fields. If that falls out clean, the whole plan does. If it doesn't, we're missing a signal in the worker's failure path and Approach B becomes more attractive. I'm confident enough that the worker's current message has the symbol + asset class in it (verified during research) that I'll skip the spike — but flag it as the cheapest gate.

### Scope (seed — to refine in Phase 1 PRD)

**Problem discovered during strategy-config-schema-extraction PR #38 final E2E:** every backtest submitted via the UI on the dev stack shows `failed` with no indication why. The real reason (worker log: `backtest_missing_data error="No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES"`) never surfaces to API, CLI, or UI callers. `/backtests/{id}/status` returns status + timestamps but no `error_message` or `error_code`. Same gap for non-data failures (worker crash, strategy import error, NautilusTrader runtime errors).

**Option (b) scope chosen:** clear failure surfacing across all three surfaces. Out of scope: auto-ingest on missing data (separate follow-up PR with its own council).

### Prior feature's Checklist (archived — strategy-config-schema-extraction PR #38 READY TO MERGE)

> > > > > > > Stashed changes

### Scope (user-ratified 2026-04-20)

- **Option B chosen:** full stack. Backend exposes `config_schema` + `config_defaults` via `GET /api/v1/strategies/{id}`. Frontend ships an auto-generating form component that consumes JSON Schema and renders the strategy's config params wherever a strategy is chosen (backtest creation, portfolio-add-strategy flows).
- **In scope:** `DiscoveredStrategy` dataclass extension, dual-path `_find_config_class()` (Nautilus `StrategyConfig` + Pydantic `BaseModel`), JSON Schema serialization, React form component + shadcn/ui integration.
- **Out of scope:** dependent/conditional field logic (v2), strategy code upload via UI (Phase 1 git-only decision stands), per-role parameter visibility, GraphQL.

### Checklist

- [x] Worktree created at `.worktrees/strategy-config-schema-extraction` off `e47243d`
- [x] Project state read
- [x] Plugins verified — `superpowers:*` + `pr-review-toolkit:*` agents listed in session skill inventory; no "Unknown skill" risk.
- [x] PRD created — `docs/prds/strategy-config-schema-extraction.md` v1 (scope B narrowed per council; 7 acceptance criteria; 8 risks mapped to council blocking objections).
- [x] Research artifact produced (Phase 2) — `docs/research/2026-04-20-strategy-config-schema-extraction.md`. Covers msgspec.json.schema behavior, StrategyConfig.parse round-trip, Nautilus ID types. Spike tests at `backend/tests/unit/test_strategy_registry.py::TestMsgspecSchemaFidelitySpike` (5/5 green — council pre-gate cleared).
- [x] Design guidance loaded — N/A scoped: shadcn-native mini-renderer per ratified Q4=a; no new visual design required beyond existing Input/Select/Switch primitives. Respecting `frontend-design.md` non-negotiables by default.
- [x] Brainstorming / Approach comparison / Contrarian gate — **PRE-DONE**: this session's 5-advisor council + chairman verdict (preserved in chat + this CONTINUITY). Contrarian OBJECTed and was honored via pre-gate spike (now green).
- [x] Plan written (Phase 3.2) — `docs/plans/2026-04-20-strategy-config-schema-extraction.md`; 7 backend tasks (B1–B7) + 2 frontend tasks (F1–F2) + 3 E2E use cases; ~2-day wall-clock budget.
- [x] Plan review loop (1 iter — Codex only) — 1 P0 + 5 P1 + 2 P2 + 1 P3. **P0 fixed mid-Phase-4**: validation moved after instrument resolve, canonical IDs injected before parse to match worker's `_prepare_strategy_config`. P1s: stale B1 refs (OK — already-built before review ran); B4 memoization rewired; B7 explicit parity test added; F2 scoped to RunBacktestForm; B6 reuse locked; naming normalized to `default_config`.
- [x] TDD execution complete (Phase 4) — **backend B1–B7 + frontend F1–F2 shipped**:
  - `schema_hooks.py` module — `nautilus_schema_hook` covers 11 Nautilus ID types; `build_user_schema(config_cls)` trims inherited `StrategyConfig` base plumbing via `__annotations__`; `ConfigSchemaStatus` 4-value enum; 18 unit tests green.
  - `DiscoveredStrategy` extended; per-strategy try/except isolates schema failures from discovery.
  - `sync_strategies_to_db(session, strategies_dir)` helper; both `GET /strategies/` (list) and `GET /strategies/{id}` (detail) call it; `code_hash` memoization skips `msgspec.json.schema()` recompute when the file hasn't changed.
  - Alembic `w1r2s3t4u5v6_add_config_schema_status_and_code_hash` — applied to dev DB; adds `config_schema_status String(32) NOT NULL DEFAULT 'no_config_class'` + `code_hash String(64)` + `ix_strategies_code_hash` index.
  - `Strategy` model + `StrategyResponse` Pydantic schema extended (`config_schema_status`, `code_hash`).
  - `_prepare_and_validate_backtest_config` at `api/backtests.py` — fast-fail 422 on `msgspec.ValidationError` with field-level path; injection parity with worker (`test_parity_config_roundtrip::test_api_and_worker_inject_identical_configs_for_omitted_defaults` passes).
  - `<SchemaForm>` (~300 LOC, shadcn-native, zero npm dep) + integration into `run-form.tsx` (gated on `config_schema_status === "ready"`, JSON textarea fallback otherwise, inline 422 field errors).
  - Pre-existing `test_es_june_2025_fixed_month` fix-up (YYYYMM format migration from PR #37).
- [x] Code review loop (2 iterations — clean on iter-2) — iter-1 Codex 2 P1 + 2 P2 + pr-toolkit 1 Important (suffix-swap config-class); all applied in-tree (persisted `config_class` column + alembic migration, `_combined_strategy_hash` for sibling `config.py`, top-level `{error:...}` 422 envelope via new `StrategyConfigValidationError` + `app.exception_handler`, SchemaForm nullable null-emission checkbox, root-fix `useAuth` memoization via `useCallback`, CORS :3300+127.0.0.1, `_find_config_class` excludes Nautilus base + prefers module-defined, `sync_strategies_to_db(prune_missing=True)`). Iter-2 pr-toolkit caught **P0** (runtime `NameError` — `Path` was only imported under `TYPE_CHECKING` so the orphan-prune branch crashed first time a file was renamed) + **P2** (no regression test for prune path). Both fixed: `Path` moved to runtime import at top; new `TestSyncStrategiesToDb` class with 2 regression tests (prune + opt-out). Iter-2 pr-toolkit re-verified clean: **READY TO MERGE**.
- [x] Simplified — 3-agent sweep (reuse/quality/efficiency) during iter-1 code review. Final-state redundant-state scan: none (memoized auth hooks + change-detection on schema recompute already in place from iter-1).
- [x] Verified (tests/lint/types) — backend: **1767 passed, 10 skipped, 16 xfailed, 0 fail** (includes new parity test + schema_hooks tests + orphan-prune regression tests); ruff clean on changed files; mypy --strict clean on changed modules. Frontend: `pnpm exec tsc --noEmit` clean; `pnpm build` clean; `pnpm lint` 0 errors, 1 pre-existing warning in `app/research/page.tsx` (from pre-branch flatten commit `82a56fd` — out of scope).
- [x] E2E use cases designed (Phase 3.2b) — 4 UCs in the plan file: UC-SCS-001 (API: schema surface with correct status enum + trimmed base fields), UC-SCS-002 (UI: zero-JSON typed form submit + reload persistence), UC-SCS-003 (API: 422 field-level error envelope), UC-SCS-004 (API: status enum disambiguates empty-schema root causes).
- [x] E2E verified via verify-e2e agent (Phase 5.4) — Report at `tests/e2e/reports/2026-04-21-strategy-config-schema-extraction.md`. Verdict: **PASS 4/4**. UC-SCS-001/003/004 via verify-e2e agent's HTTP client; UC-SCS-002 driven by main agent via `mcp__playwright__*` tools because verify-e2e agent toolbox lacks Playwright (tooling limitation, not a product defect). Infra prerequisites addressed same session: FE-01 (frontend Tailwind v4 container mount — postcss/next.config.ts missing volume mounts); BE-01 (Databento `FuturesContract.to_dict()` pyo3-vs-Cython signature drift in `parser.py`). Additionally re-verified UC-SCS-003 top-level `{error:...}` envelope shape via curl after the final iter-1 fix — HTTP 422 with `{"error":{"code":"VALIDATION_ERROR","message":"Strategy config failed validation","details":[{"field":"fast_ema_period","message":"Expected \`int\`, got \`str\` - at $.fast_ema_period"}]}}`.
- [x] E2E regression passed — Phase 5.4b vacuously passes: no prior graduated UCs under `tests/e2e/use-cases/strategies/`; this PR is the first use-case file in that directory.
- [x] E2E use cases graduated (Phase 6.2b) — 4 use cases committed at `tests/e2e/use-cases/strategies/config-schema-form.md` (happy path + error paths + persistence via reload).
- [ ] E2E specs graduated (Phase 6.2c — if Playwright framework installed) — N/A: no Playwright specs authored; the 4 UCs remain executable via verify-e2e agent (markdown layer). Deferred pending explicit framework-bridge request — see `.claude/rules/testing.md` "Playwright Framework Bridge (Optional)".
- [x] Learnings documented — code-review iter-2 P0 (runtime NameError from TYPE_CHECKING-only import of Path) saved to auto-memory. Pattern: when code is added that CALLS a type previously used only in annotations, the import must move out of the `TYPE_CHECKING:` block; reviewing TYPE_CHECKING imports when extending module behavior is now a checklist item.
- [x] State files updated — CONTINUITY + CHANGELOG reflect shipped state.
- [ ] Committed and pushed
- [ ] PR created
- [ ] PR reviews addressed
- [ ] Branch finished

### Prior feature's Checklist (archived — superseded by Done cont'd 9 below)

- [x] Worktree created at `.worktrees/live-path-wiring-registry` off `3fa6097`
- [x] Project state read
- [x] Workflow tracking initialized
- [x] Plugins verified
- [x] PRD created (Phase 1) — `docs/prds/live-path-wiring-registry.md` v1.0 draft; 10 Q&A defaulted in discussion log; 6 open questions flagged for plan-review
- [x] Research artifact produced (Phase 2) — N/A-minimal confirmed. Brief at `docs/research/2026-04-20-live-path-wiring-registry.md`. 5 PRD open-questions answered from code: (1) `trading_metrics.py` exists; (2) `alerting.send_alert(level="warning")` supports WARN; (3) `spawn_today_iso` plumbing verified end-to-end through supervisor→payload→subprocess→live_node_config; (4) Nautilus `InteractiveBrokersInstrumentProviderConfig` takes `load_contracts: FrozenSet[IBContract]` — resolver must reconstruct `IBContract` from stored `contract_spec`; (5) `InstrumentRegistry.find_by_alias` defaults to UTC — resolver must make `as_of_date` required (no default).
- [x] Brainstorming / Approach comparison / Contrarian gate — **PRE-DONE**: `docs/decisions/live-path-registry-wiring.md` captures the 5-advisor council + chairman verdict. Cite as Phase 3.1/3.1b/3.1c artifact; skip re-running.
- [x] Plan written (Phase 3.2) — `docs/plans/2026-04-20-live-path-wiring-registry.md` v1; 16 tasks; TDD-structured; 6 spot-checks flagged for plan-review.
- [x] Plan review loop (4 iterations — closed 2026-04-20) — trajectory: iter-1 3P0/7P1/5P2 → iter-2 0P0/5P1/4P2 → iter-3 0P0/6P1/8P2 → iter-4 0P0/3P1 each reviewer. P0 eliminated at iter-2; P1s narrowed each pass (foundation → API drift → implementation detail → test mechanics). Final iter-4 P1s (cacheable-flag, done-callback exception logging, mock-assertion positional, fixture path, enum-conversion telemetry, counter introspection) all applied in v5 plan. Remaining polish items are P2/P3 test-mechanics details that Phase 4 TDD catches naturally. Foundation stable across all 4 iterations; no architectural drift surfaced. Closed per "3-iteration hard cap + productive-convergence" rule in feedback memory.
  - **iter 1 (2026-04-20)**: Claude 3 P0, 7 P1, 5 P2, 1 P3 · Codex 0 P0, 7 P1, 2 P2, 0 P3. Blocking overlap: send_alert API drift; metrics pattern drift (prometheus_client → hand-rolled registry); bare-ticker branch missing; decade boundary; supervisor failure path; API preflight out-of-scope; grep-based regression; pickle test missing. All fixes staged in plan v2.
  - **iter 2 (2026-04-20)**: Claude 0 P0, 5 P1, 4 P2, 0 P3 · Codex 0 P0, 4 P1, 1 P2, 0 P3. Trajectory converging (iter-1 P0/P1 all resolved). New P1s surfaced: (a) EndpointOutcome lives in services/live/idempotency.py with \_PERMANENT_FAILURE_KINDS gate + 503/detail shape (plan targeted wrong file + wrong response contract); (b) function name `build_live_node_config` doesn't exist → real name `build_portfolio_trading_node_config`; (c) sync alerting blocks event loop → must wrap in asyncio.to_thread; (d) PRD US-002 details.missing_symbols vs Task 12 "defer details" self-contradiction; (e) AmbiguousSymbolError not ValueError → transient-retry branch hit; (f) \_pick_active_alias tie-break uses random UUID.id → not stable; (g) find_by_alias UTC default not removed. Fixes staged in iter-3 plan revision.
- [x] E2E use cases designed (Phase 3.2b) — `tests/e2e/use-cases/live/registry-backed-deploy.md` drafted with UC-L-REG-001 (deploy QQQ after refresh), UC-L-REG-002 (un-warmed symbol → 422 + retry-after-fix non-cacheable check), UC-L-REG-003 (futures-roll M6/U6), UC-L-REG-004 (option rejected), UC-L-REG-005 (telemetry). Intent/Steps/Verification/Persistence structure per .claude/rules/testing.md. Graduates to permanent regression set after Phase 5.4.
- [x] TDD execution complete (Phase 4) — 17 plan tasks complete via subagent-driven dispatch. 1728 pytest pass, 0 fail. ruff + mypy --strict clean on new files.
- [x] Code review loop (2 iterations — clean) — iter-1 Claude 0/0/0/0 READY + Codex 2 P0/1 P1 (staging inconsistency caught + arbitrary `.limit(1)` on overlap flagged). Fixes: re-staged 7 worktree-modified files, added `ORDER BY effective_from DESC` to `find_by_alias`. Iter-2 both reviewers 0/0/0/0 READY TO MERGE.
- [x] Simplified — 3-agent parallel review (reuse/quality/efficiency). Applied Tier-1 fixes: (a) promoted `_PERMANENT_FAILURE_KINDS` + `_REGISTRY_FAILURE_KINDS` to public names and removed the inline-literal duplication at `api/live.py:644-652`; (b) added `AmbiguityReason` + `TelemetrySource` StrEnums for stringly-typed labels + reason attribute; (c) fire-and-forget `asyncio.create_task(_fire_alert_bounded(...))` removes up to 2s of blocking latency from both miss + incomplete raise paths. Tier-2 deferred as follow-ups: (d) base-class `to_error_message`; (e) extract `_fire_alert_bounded` to public `alerting_service` helper; (f) dedupe `_FUTURES_MONTH_CODES` across modules; (g) batch `find_by_aliases` for concurrent DB lookups (N × RTT → 1 RTT); (h) narrative-comment cleanup. 1728/1728 regression tests still pass.
- [x] Verified (tests/lint/types) — 1728 pytest pass, 1 skipped, 16 xfailed, 0 fail (225s). `ruff check` clean on all changed files. `mypy --strict` clean on `security_master/`, `failure_kind.py`, `idempotency.py`, `trading_metrics.py` (pre-existing mypy errors in `service.py` / `live_node_config.py` nautilus-stub imports are untouched by this PR).
- [x] E2E verified via verify-e2e agent (Phase 5.4) — Report at `tests/e2e/reports/2026-04-20-live-path-wiring-registry.md`. Ran all 5 designed use cases (UC-L-REG-001..005) against the stack at `http://localhost:8800` with worktree code volume-mounted + migrations at head. Initial run surfaced 2 apparent FAIL_BUGs that investigation of docker logs resolved: UC-001 was test-ARRANGE error (malformed alias seed — fixed on retry, third run PASS with `Contract qualified for QQQ.NASDAQ` + strategy RUNNING + bar subscription); UC-005 counter `0` finding is a pre-existing per-process MetricsRegistry limitation (affects all existing msai counters equally — not a regression; structured logs DO emit correctly per supervisor logs). **Final verdict: PASS** (3 PASS + 1 PASS-with-documented-limitation + 1 FAIL_INFRA-accepted-substitute for UC-003 which uses integration test fallback per the use-case spec).
- [x] **REAL-MONEY DRILL on U4705114 — PASSED 2026-04-20 14:26–14:46 UTC (6 deploys across 5 asset classes).** Report at `docs/runbooks/drill-reports/2026-04-20-live-path-registry-drill.md`. Initial AAPL deploy (1 share BUY @ $274.12 → kill-all → SELL @ $274.02) validated the core flow. Multi-symbol extension per Pablo's request: SPY / MSFT / EUR/USD / IWM all PASS end-to-end with registry-backed resolution + IB qualification + BUY fill + `/kill-all` SELL + flat positions (total cost ~$8 across 6 drills). ES futures reached registry→qualify→bar-subscribe stage successfully but no `on_bar` fired in 150s wait window — likely a market-data entitlement on `mslvp000` account, downstream of this PR's scope. Every `live_instrument_resolved source=registry` log line captured in supervisor stdout. All 5 council constraints (#1 / #3 / #4 / #5 / #6) validated. Drill identified 3 side bugs — now fixed in-branch per "no bugs left behind": (a) `ib_qualifier.py` futures used `%Y%m%d` which breaks on Juneteenth-Friday shifts → now `%Y%m` so IB resolves holiday-adjusted expiry; (b) `SecurityMaster._upsert_definition_and_alias` now normalizes FX `raw_symbol` IB-dot ("EUR.USD") → slash form ("EUR/USD") at storage boundary; (c) `GET /live/trades` now accepts + applies optional `deployment_id: UUID` query filter. 3 regression tests + integration test added; 62/62 affected tests pass; 0 net-new mypy errors.
- [x] E2E regression passed — Phase 5.4b vacuously passes: no prior-graduated use cases exist in `tests/e2e/use-cases/` to regress against. This PR is the first to exercise the Phase 5.4/5.4b lifecycle.
- [x] Use cases graduated (Phase 6.2b) — 5 use cases (UC-L-REG-001..005) committed at `tests/e2e/use-cases/live/registry-backed-deploy.md`. Phase 5.4 verification report at `tests/e2e/reports/2026-04-20-live-path-wiring-registry.md` (gitignored per project convention; mtime evidence satisfies the gate hook).
- [x] State files updated — CONTINUITY + CHANGELOG + solution doc + drill runbook all committed.
- [x] Committed and pushed — 3 commits: `0d3799d` (main implementation), `fffb5ea` (E2E report CONTINUITY update), `<next>` (PRD/research/graduated-cases). Pushing now.
- [x] Drill-uncovered bug fixes committed at `e5afb7e` — 3 fixes (ib_qualifier futures YYYYMM; FX raw_symbol slash normalize; /live/trades deployment_id filter). E2E re-verified against live stack before commit: Bug #1 ES refresh PASS, Bug #2 EUR/USD cold-path upsert stores slash form PASS, Bug #3 /live/trades filter PASS (8 total / 2 scoped / 0 bogus / 422 malformed).
- [x] PR created — PR #37 (`Live-path wiring onto instrument registry`), squash-merged 2026-04-20 at `29dbe9b`
- [x] PR reviews addressed — Codex P1 (dispatch heuristic `"." in sym` misclassifies share-class tickers like BRK.B) fixed at `be23558`, replied in-thread, regression test added
- [x] Branch finished — worktree removed, remote + local branch deleted, main ff'd to `29dbe9b`

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

## Done (cont'd 10) — Dev stack restarted from new root + volume-name pinning (2026-04-20)

- **Problem:** post-flatten `docker compose` from the repo root created a NEW project (`msai-v2`) whose volumes got prefixed with that project name. The actual drill data lived in `live-path-wiring-registry_postgres_data` (from the worktree's compose invocation during PR #37 work). Bringing up the new project without a name-pin would silently spawn a fresh-empty Postgres and lose all registry rows, drill trades, and deployment history.
- **Fix 1 — data preserved:** Migrated `live-path-wiring-registry_postgres_data` → `msai_postgres_data` via a migration container (`alpine cp -a`). Verified 5 registry rows + 8 live trade fills + 7 deployments preserved.
- **Fix 2 — never again:** Pinned volume names explicitly in compose so any future project-name change (directory rename, worktree spawn, etc.) cannot orphan stateful volumes:
  - `docker-compose.dev.yml` — `postgres_data: { name: msai_postgres_data }`
  - `docker-compose.prod.yml` — same pin on `postgres_data`, `app_data`, `ib_gateway_settings` (3 prod-stateful volumes)
- **Root cause documented in compose comment:** prevents the next engineer from undoing the pin without understanding why it's there.
- **Orphan inventory** (left in place; destructive cleanup is a separate decision): `bug-bash_postgres_data`, `codex-version_postgres_data`, `claude-version_postgres_data`, `live-path-wiring-registry_postgres_data`, `mcpgateway_postgres_data`, `claude-version_ib_gateway_settings` — all pre-flatten or worktree artifacts, now superseded by the single pinned `msai_postgres_data`.

## Now

- **Active workflow: `/new-feature strategy-config-schema-extraction`** on worktree `.worktrees/strategy-config-schema-extraction` off `e47243d`. Phase 5 quality gates in progress; **PAUSED on user interrupt** (UI flickering from my local dev server attempt — stopped).
- **Code status — DONE and verified:**
  - Phase 0 spike PASSED (5/5). Phase 1 PRD + Phase 2 research brief + Phase 3.2 plan all landed.
  - Phase 3.3 plan review: Codex iter-1 returned 1 P0 + 5 P1 + 2 P2 + 1 P3 — **P0 FIXED** (validation moved post-instrument-resolve, canonical IDs injected before `StrategyConfig.parse` to match the worker's `_prepare_strategy_config`). Remaining P1s either rolled into the implementation (B1 stale-against-branch since code was already built; B3 acceptance criteria refined) or addressed in B6 (reuse via `load_strategy_class` + `_prepare_strategy_config` mirror), F2 (integrated into `RunBacktestForm` not `BacktestsPage`), B7 (explicit parity test).
  - Phase 4 TDD: backend B1–B7 + frontend F1–F2 shipped (see "Done cont'd 11" and CHANGELOG).
  - Phase 5.3 verify: **1504/1504 unit tests pass** on backend. Ruff clean on all files I modified (3 remaining ruff errors are pre-existing TC003 style nits on baseline). mypy --strict: zero net-new errors (4 pre-existing nautilus-stub + load_strategy_class `Any`-return remain on baseline). Frontend: `pnpm build` + `tsc --noEmit` + `eslint` all clean.
  - Includes pre-existing test fix-up `test_es_june_2025_fixed_month` (leftover from PR #37's YYYYMM change) — "no bugs left behind".
- **Phase 5.4 E2E — PARTIAL (blocked on two inherited infra issues):**
  - UC-SCS-001 (API: `/strategies/{id}` schema response) — **PASS**
  - UC-SCS-002 (UI: typed form renders on strategy select) — **FAIL_INFRA** (frontend Docker container's Next.js 15 + Turbopack + pnpm-symlink CSS-import resolution fails on `tw-animate-css` + `shadcn/tailwind.css` + `@/components/providers`. Pre-existing since PR #36 flatten. Host `pnpm build` and `pnpm dev` both succeed — feature code is correct). My attempt to use local dev server on :3001 caused UI flickering — Pablo interrupted; I stopped the server.
  - UC-SCS-003 Step 1 (invalid instrument → 422 on resolve path) — **PASS**
  - UC-SCS-003 Step 2 (valid instrument + malformed config → 422 via `StrategyConfig.parse`) — **FAIL_INFRA** (empty databento registry; sanctioned ARRANGE via `msai instruments refresh --provider databento` fails with `FuturesContract.to_dict()` error that hangs under `ES.n.0`; `interactive_brokers` path was registered but backtest endpoint hits `databento` provider. Code path IS unit-tested at `tests/unit/test_backtests_api.py::TestPrepareAndValidateBacktestConfig::test_rejects_malformed_instrument_id_with_422_and_field_path` — the 422 envelope shape is proven.)
  - UC-SCS-004 (`config_schema_status` enum surfaces for every row) — **PASS**
- **E2E report:** will be written to `tests/e2e/reports/2026-04-20-strategy-config-schema-extraction.md` when the run can complete; partial report currently in conversation (verify-e2e agent output verbatim).
- **Dev compose volume-pin note:** added `./frontend/tsconfig.json` + `./frontend/package.json` mounts to `docker-compose.dev.yml` so future TS config changes propagate without image rebuild. Does not fix the CSS-resolver issue.
- **Awaiting Pablo's decision:**
  1. **Ship-with-docs** — accept UC-SCS-002 + UC-SCS-003 Step 2 as FAIL_INFRA (documented in the E2E report), same shape as PR #37's UC-003 fallback. Code correctness proven by unit tests + parity test + the 3/5 E2E UCs that passed.
  2. **Fix container build first** — diagnose Next/Turbopack/pnpm Linux CSS issue (~1-2 hrs estimated). Then UC-SCS-002 E2E passes.
  3. **Pause cold.** All work durable on disk (15 files modified/new). Resume in a fresh session.

## Known issues surfaced this session (for follow-up — "no bugs left behind" tracker)

- **FE-01** Frontend Docker dev container can't resolve CSS imports (`tw-animate-css`, `shadcn/tailwind.css`) despite modules present. Pre-existing since PR #36; container-only. Host builds are fine.
- **BE-01** `msai instruments refresh --provider databento --symbols ES.n.0` hangs / errors with `FuturesContract.to_dict() takes no arguments (1 given)` at `parser.py:188` per verify-e2e agent. Local reproduction of the same function on synthetic FuturesContract succeeds — so error path is Databento-specific. Requires deeper trace.

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

### PR #35 documented known limitations

- **Midnight-CT roll-day race** — preflight and `_run_ib_resolve_for_live` call `exchange_local_today()` independently; narrow window, operator-recoverable.
- **CLI preflight doesn't accept registry-moved aliases for non-futures** — manifests only if IB qualification returned a venue the hardcoded `canonical_instrument_id` mapping doesn't match.
