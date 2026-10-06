# Plan: Nautilus V2 research integration

**Status:** Proposed implementation; PRD v1.0 explicitly approved 2026-10-06. Production edits wait for clean independent plan review.
**Immutable workflow base ref:** `quick-fix/october-release-status`
**Immutable workflow base SHA:** `9d51a633ebeeff9b85ee06b6085df056dbe68458`
**Worktree / branch:** `.worktrees/nautilus-v2-research` / `feat/nautilus-v2-research`.

## Goal and evidence boundary

Complete the approved [PRD](../prds/nautilus-v2-research.md): discover the existing EMA strategy, validate its actual config, execute a native V2 minute-equity backtest, reconcile and persist results, inspect/report/revisit through API → CLI → real browser, and preserve training selection/Discovery. Keep baseline code, raw data, catalogs and saved results recoverable. No push, PR, merge, Azure installation, vendor purchase/download or broker operation is authorized here.

[Fresh research](../research/2026-10-06-nautilus-v2-research.md) selects exact `nautilus-trader==2.0.0rc6`, commit `7b766f8825b2539c5b2ac1375e9d97b41c509edb`, Linux cp312 wheel SHA256 `9b4002a7bf5e6399c51073039b740ccf3ca7a1e2584ff72c7d479f03eaa9658d`. Preserve other locked dependencies where compatible, declare application Msgspec explicitly at its existing 0.20.0 version, remove the absent `ib` extra. No rolling develop dependency or default 1.231 bridge. Refresh release identity before promotion; a newer release is information, not permission to change the pin.

The [completed native trial](../research/2026-10-05-nautilus-runtime-trial.md) is retained, including same-bar cash failure, aggregate margin overcommitment and unexplained second-sell one-tick difference. New acceptance uses real application execution on **labeled synthetic data**, since the fresh local inventory has no Docker images/containers/volumes, backend virtualenv, frontend dependencies or market Parquet. It cannot certify market completeness, realistic costs, strategy alpha, joint portfolios or live capital. Historical Azure results remain historical; do not imply a new readback.

## Approaches and selection

| Approach | Complexity / blast radius | Reversibility / validation | User and correctness risk |
| --- | --- | --- | --- |
| A. One pinned V2 candidate application, supported research mode, explicit refusal of unsupported live/vendor operations | Port the owning native research adapters and necessary startup imports; retain existing REST/CLI/DB/Redis/Next.js workflows. No engine router. | Isolated Linux images/state plus preserved V1 image/code/catalog; test the real app directly. | Unsupported live functions must refuse before I/O; release readiness cannot imply a deployable live replacement. |
| B. Keep V1 application/live environment, launch a separately packaged V2 research worker with a process result contract | Smaller live import blast radius, but two dependency images, config/strategy representations and orchestration/deployment paths during the slice. | Separate environment is reversible; validating provenance and worker selection takes additional work. | Routing mistakes could run V1 while labeling V2; duplicated strategy/config boundary complicates acceptance. |

Choose A for the isolated development candidate. B is viable as a bounded temporary evaluation environment but adds an unneeded dual-engine product path. Keep the accepted V1 release outside the candidate. **This candidate is research-only and is not a drop-in live release.** A later live migration or separately approved release decision is required before installation. Do not conceal that restriction behind healthy generic liveness.

## Architecture and supported contracts

Keep FastAPI → arq → common `BacktestRunner` spawned process → native `BacktestNode` → existing PostgreSQL/report persistence → API/CLI/browser. Keep DB schema and existing jobs intact; optional JSON accounting/provenance fields carry new assumptions. MSAI still owns selection, orchestration and the raw-Parquet bridge.

1. **Config boundary:** use native `StrategyConfig` subclasses with explicit typed constructors, port tracked examples and actual EMA strategy imports. Discovery recognizes actual subclasses instead of `.parse` presence. Build one small schema/validation helper from declared constructor/type annotations using existing Pydantic machinery and narrow native ID/BarType/Decimal conversions. Reject missing/unknown/invalid fields before queueing, retain defaults and useful field errors, and let native constructors validate native base settings. Avoid duplicate validation implementations across discovery/API/runner. Native RC6 swallows extra constructor kwargs, so relying on native construction alone fails the acceptance criterion. This is the concrete native gap; no general schema framework or permanent V1 compatibility branch.
2. **Native catalog:** construct explicit supported Equity metadata and native `BarDataWrangler` IPC batches with Decimal128(38,16) OHLCV and UTC nanosecond timestamps; use typed catalog write/query/interval operations. Stream existing bounded batches. Keep minute-equity scope, reject unsupported asset/interval instead of substituting Equity. Preserve source marker/reuse/rebuild behavior, isolate `nautilus-v2` catalog namespace, and never purge baseline/sibling paths. Native catalog is authoritative; no custom interval scanner.
3. **Runner:** build native configs, call `build`, then `add_strategy_from_config` for the actual strategy and `run(raise_exception=True)` with native lifecycle settings. Extract native orders/fills/positions/account reports through node APIs before disposal. Keep inclusive UTC date normalization and spawned process cancellation. Missing engine results/reports must fail, not silently emit zero success. Native fee/fill/venue config is explicit: USD, configured opening capital, leverage 1, deterministic fill seed, zero modeled commission/slippage for the default representative run. A bounded test configuration uses native FixedFeeModel($1/fill), $10,000 capital for the independently calculated $28 example; no custom fee/fill/account ledger. Record actual settings, engine pin and account-return basis in optional accounting JSON. Keep `realized_account_balance` daily analytics until native marked-equity coverage/basis is independently accepted. No silently changed legacy basis.
4. **Startup/live boundary:** make deferred live/vendor imports lazy only where needed to express the supported research scope. A shared capability check identifies V2 research mode from the installed engine pin; it cannot be overridden into supported live mode by an environment flag. Authenticate protected operations first. Starting/resuming live trading and live cold-cache reads explicitly refuse before broker/native V1 process execution. Existing persisted status/stop/halt controls remain usable; never weaken exit controls or authorization. Release-readiness identifies this candidate as unsupported for live installation, even with zero active deployments. API startup refuses incompatible existing active live state. Supervisor invocation emits an explicit unsupported-runtime result without connecting/spawning; it is a documented excluded service, not a claimed passing V2 supervisor. Projection must not silently consume incompatible V2 event streams. Candidate services are API, backtest/research/portfolio/ingest workers and frontend; ingest can start but vendor definition/live adapter operations remain explicitly unaccepted/refused until ported. Preserve direct Databento SDK raw bar ingestion where unchanged and compatible.
5. **Product results:** preserve existing optional bar count/unknown meaning, ratio units, fill identities and legacy account notices. New result details, CLI JSON and report show engine, capital, leverage, fee/fill model and synthetic/limited interpretation in plain language. Keep report capability/auth protections. Do not rewrite saved jobs to add assumed zero costs. Keep PR108 training-only selection, latest training window, exploratory Discovery provenance and refusal unchanged, verified at the common runner boundary.

## Exact tasks, ownership and TDD

Execute with the exact `forge-v6-producer` role, immutable base SHA and actual runtime task ID. Producers are not alone in this worktree; do not revert other edits. Each task owns the paths below, follows RED → GREEN → refactor, performs one spec and one quality check, and writes strict task receipts. Root coordinates integration; paths shared across tasks change only in dependency order. Any additional file needs a concrete owning import/test failure and a recorded plan amendment before broadening production scope.

### T0: Sanctioned fixture and baseline capture prerequisite

Before T1 changes dependencies, own `scripts/seed_market_data.py`, `docs/runbooks/nautilus-v2-research.md` (new), a small fixture registration helper used only by that documented command if needed, and `backend/tests/unit/test_seed_research_fixture.py` (new). Extend the existing documented synthetic seed command rather than manually injecting files/state during E2E. Give it a fixed deterministic seed and explicit UTC timestamps; synthetic weekdays are not exchange-calendar completeness. Provide an explicit development-only registry bootstrap using `SecurityMaster._upsert_definition_and_alias` in the owning application transaction. Pre-acquire the same advisory lock, refuse production, non-isolated targets and pre-existing unmarked/conflicting definitions/aliases across providers, especially IB aliases. Only create the Databento-carrier research alias AAPL.NASDAQ, never instantiate provider clients/downloads or mark live qualification. Do not invent a synthetic provider the resolver cannot use. Existing models have no synthetic provenance field: create a deterministic manifest with identity/data hashes and synthetic scope, and propagate that origin into new experiment provenance under T4/T5. Idempotence requires matching manifest/metadata. Test safety/refusal/idempotence with the existing command as the supported setup boundary; no verifier SQL escape.

Build the immutable V1 image first (already done). Mount the extended documented setup script read-only, use the baseline image's own interpreter/application imports and isolated DB/data volume, then create/capture actual saved baseline results through API/CLI before dependency changes. This preserves immutable baseline application code while allowing a documented setup command that calls its existing writer. Capture public payload/report hashes and representative behavior traces; save the V1 catalog/code/image and isolated database snapshot. T1 can start only after this capture; T4 consumes it. Final restoration/recovery acceptance remains in T5. Do not claim the original seed's Parquet-only behavior bootstraps the registry.

### T1: Reproducible environment and research capability boundary (after T0)

Own `backend/pyproject.toml`, `backend/uv.lock`, `backend/src/msai/main.py`, `backend/src/msai/api/live.py`, `backend/src/msai/api/live_deps.py`, `backend/src/msai/api/websocket.py`, `backend/src/msai/services/nautilus/runtime_capabilities.py` (new), `backend/src/msai/live_supervisor/__main__.py` and `main.py` startup boundary only, `backend/src/msai/services/data_sources/databento_client.py`, `backend/src/msai/services/nautilus/security_master/continuous_futures.py`, `backend/src/msai/services/nautilus/live_instrument_bootstrap.py`, and owning `backend/tests/unit/test_nautilus_runtime_capabilities.py` (new), startup/live route tests as necessary. The shared `exchange_local_today` helper must be native-independent and importable without initializing IB contracts/config. Defer broker-only imports AND module-level V1 contract/config construction into their owning live helpers. This covers `api/symbol_onboarding.py`'s date-helper import, `SecurityMaster._upsert_definition_and_alias` and pytest collection through `tests/conftest.py`. Do not rewrite live node configuration/projection/event formats. The API must refuse incompatible operations explicitly before removed imports, I/O or enqueue/spawn. Test unauthenticated requests still refuse auth, forbidden starts/resumes and cold reads create no broker/process/command effect, stop/halt remain reachable, release-readiness declares limitation, API lifespan and all relevant worker imports succeed. Resolve lock minimally and confirm exact wheel identity. Clean install plus Linux startup is required later, not inferred from mocked tests.

### T2: Native strategy/config discovery (parallel with T1)

Own `backend/src/msai/services/strategy_registry.py`, `backend/src/msai/services/nautilus/strategy_loader.py`, `schema_hooks.py`, `backend/src/msai/api/backtests.py` config preparation/validation only, `strategies/example/config.py`, `ema_cross.py`, `cycle_buy_sell.py`, `cycle_buy_sell_b.py`, `smoke_market_order.py`, `strategies/intentionally_failing_strategy.py`, and owning registry/loader/schema/config tests. Use native model/trading exports and explicit config constructors; same fields/defaults and engine-injected instrument/bar identity. Test actual discovery, required/default typed fields, malformed IDs, extra typo, negative/invalid periods and size, and execution-safe native construction. Keep helper/config hash inputs and governance. Document V2 mean-seeded EMA behavior, do not force matching P&L by replacing the indicator. Ensure the risk mixin is inert only in verified backtest scope; unsupported live refuses earlier and may not be presented as migrated quantitative risk.

### T3: Native instruments/catalog (after T1 dependency install; can overlap T2)

Own `backend/src/msai/services/nautilus/catalog_builder.py`, `instruments.py`, `backend/src/msai/core/config.py` catalog-root property only, `backend/tests/unit/test_catalog_builder_streaming.py`, `backend/tests/unit/services/nautilus/test_catalog_builder.py`, and new native integration coverage as needed. RED tests native exact OHLCV precision, timestamp/order/count/readback, inclusive single-date bounds, native coverage/gaps, idempotent rebuild and baseline/sibling isolation. GREEN typed native APIs plus narrow Arrow bridge. Do not broaden equity to futures/options/crypto. Expose actionable unsupported/corrupt/missing input failures.

### T4: Actual native runner and persisted economics (after T1–T3)

Own `backend/src/msai/services/nautilus/backtest_runner.py`, `backend/src/msai/workers/backtest_job.py`, `backend/src/msai/schemas/backtest.py`, existing date-window/metrics/runner/job/schema tests and `backend/tests/integration/test_nautilus_v2_research.py` (new). First RED common-runner native tiny reference: 10 shares buy100/sell103, fees2, realized net28, ending10028, return0.0028; add an open-position control showing realized balance differs from marked equity, preventing incorrect basis substitution. Then runner missing report/exception/cancellation cases, native report Decimal/index → persisted identity/units, and unchanged saved legacy shape. Keep PR109 date tests, including equal dates and reversed windows. Capture actual representative EMA callbacks/indicator initialization/signals/first-order/fills against baseline in a bounded comparison artifact; explain differences using pinned native algorithms. Keep unresolved cash/margin/tick trial cases explicit, and reject/refuse unsupported shared-capital acceptance without a shadow ledger. No passing single-strategy result closes portfolio acceptance.

### T5: Sanctioned fixture, product presentation and acceptance (after T4)

Own `docs/runbooks/nautilus-v2-research.md` final runtime/recovery instructions after T0, `frontend/src/lib/types.ts` (actual accounting type location), `frontend/src/app/backtests/[id]/page.tsx`, owning report/accounting UI tests where material, `tests/e2e/use-cases/backtests/nautilus-v2-research.md` and a graduated frontend spec only after preliminary PASS, `docs/solutions/backtesting/nautilus-v2-research.md`, and `docs/CHANGELOG.md`.

Use the documented tested seed/bootstrap from T0 unchanged for V2 acceptance. T4 binds the manifest's synthetic origin into new experiment provenance; results/report/UI must show that scope. Failure of sanctioned setup is FAIL_INFRA until repaired, never permission for hidden state edits.

Use a literal isolated local Compose configuration under `.forge/local/runtime/` with unique container/volume names and host API/UI ports (e.g.18800/13300), no root `.env` secrets, explicit known dev auth key, broker loopback65534, vendor keys empty, daily ingest disabled and auto-heal0. No broker-profile container. T0 preserves the immutable V1 image, baseline results/catalog, public payload/report hashes and database snapshot before T1. Switch only this isolated application DB to candidate services; use a separate V2 catalog. After V2 runs, restore compatible V1 code plus untouched V1 catalog against the same schema-compatible DB and verify saved baseline readback and baseline rerun through public interfaces. Original primary/Azure stores remain untouched. Do not call empty folders a preserved saved-result proof.

## Local verification and final candidate

Use focused owning suites for config, catalog, runner/date/metrics/job, startup boundary and PR108 selection/Discovery; fast local Ruff/mypy plus frontend lint/build. Test clean Python3.12 Linux image and actual API/worker startup with Postgres/Redis, then API → CLI → browser acceptance below. An exhaustive repository test run requires separate user request; no such request exists. Record skipped unrelated/live suites honestly. Actual browser computer use observes real backend state; mocked request interception cannot certify this feature. No Entra acceptance claim from the documented local auth bypass.

After preliminary feature `verify-e2e` PASS: graduate use cases/specs, finish solution/changelog, run Forge simplification, stage approved ignored artifacts and freeze one staged-clean candidate. Dispatch distinct final code-spec/code-quality reviews, verify-app and final affected E2E against that same fingerprint; mutate only after returning to repair phase and invalidate all final receipts. Promote exact candidate through Forge helper before commit. Current chat has primary checkout as host cwd, so shipping requires a task-worktree session; tool workdir alone cannot change hook context. Prepare concrete publication scope only after certification and obtain missing shipping authorization then.

## Acceptance criteria

- Exact RC6 clean lock/image; all affected research services start with supported configuration; unsupported live operations fail explicitly before I/O and cannot masquerade as deployable live readiness.
- Existing representative strategy and its config discovered, validated and executed by native node/catalog; invalid fields fail usefully before job creation and correction succeeds.
- Native tiny economic reference reconciles all fills/fees/capital/return; representative callbacks/indicator/signal/order/fill differences are documented, not hidden behind aggregate P&L.
- Same persisted V2 job has API/CLI/browser agreement on engine, dates, units, capital/cost/fill assumptions, fills and report, including reload/history.
- Baseline saved payload/report readback preserved with original semantics, and compatible code+state recovery exercised.
- Bounded real sweep and walk-forward preserve training-only selection/latest training evidence and exploratory Discovery/refusal; owning diagnostic-invariance regression remains green.
- Retained shared-cash/aggregate-margin/tick failures remain visible and unsupported cases refused; no portfolio/live/data-cost/final-validation readiness claim.

#### E2E Use Cases

**Surface coverage decision:** API, CLI and UI are all required for this cross-interface research slice. None excluded. Treat any `SURFACE_COVERAGE_WARNING` as a missed planned journey: add/repair the affected UC and rerun; do not silence it by claiming no code changed on that surface. Execute in this order. All setup commands below must be documented and pass before use; broken setup is FAIL_INFRA and requires repair, never hidden SQL/file/queue injection.

### UC-V2-001: Research operator iterates and revisits a native experiment — API

Actor: Authenticated research operator evaluating the existing EMA strategy in an isolated development environment.
Scenario: Synthetic minute-equity data and supported metadata are installed with the documented seed command. The operator needs a valid run after correcting an invalid configuration, then evidence they can revisit.
Intent: Complete and interpret a bounded strategy iteration with clear assumptions and retained results.
Interface: API.
Setup: Candidate API/Redis/Postgres/workers running on the documented isolated URLs; known dev API key configured through supported auth; documented synthetic seed command; strategy registration through GET strategies. An independently created baseline saved job ID/payload/report hash exists from the preserved V1 environment; setup does not create the candidate experiment.
Steps:
1. Discover EMA and its schema/defaults. Submit invalid size/period/unknown field, observe actionable rejection and no candidate completed job; correct it.
2. Submit EMA AAPL.NASDAQ for an inclusive single UTC fixture date and poll until completed. Follow results, paginated fills and authenticated/signed report delivery.
3. Compare engine pin, bars, dates, fills, return units and accounting assumptions; request the saved baseline job/report and compare original public observations.
Verification: Client receives a field-specific error before dispatch for invalid config and can use the corrected job ID. Response includes actual RC6 identity, explicit default economic settings, known bar/fill count, realized-account return basis and report matching the stored series. Follow-up requests return the same job and fill identities; legacy fields retain recorded/unknown meanings. Deliberately invalid dates/missing input cannot become successful zero-metric results.
Persistence: Re-request results/trades/report and history, compare the same candidate and baseline observations. Repeat after candidate application restart.

### UC-V2-002: Command-line operator runs and compares results — CLI

Actor: CLI research operator following a strategy experiment without the browser.
Scenario: The operator has the same synthetic fixture and wants to submit and revisit an experiment using the installed CLI.
Intent: Run a bounded strategy experiment and understand its financial scope from persistent command output.
Interface: CLI.
Setup: UC001 API acceptance passed; installed candidate CLI targets the same API via documented environment and known dev key; same seed fixture. Setup does not submit this CLI experiment.
Steps:
1. List/discover EMA; invoke backtest run with an invalid configuration or reversed date window and observe correction guidance; rerun with valid config/window.
2. Follow returned job ID through status/results/trades/report commands supported by the installed CLI. Compare recorded units/assumptions/engine and the API-visible reference job.
Verification: stderr explains the invalid input and stdout shows the corrected submitted/completed job with matching recorded engine, capital, dates, bar/fill counts and realized-balance return. The next invocation lists/shows the same saved job and fields; absence of legacy assumptions remains unknown.
Persistence: Re-invoke show/history/trades/report for the same ID after completion and compare saved baseline output.

### UC-V2-003: Browser operator submits, inspects and reloads — UI

Actor: Research operator using the established strategy/backtest dashboard in the isolated development environment.
Scenario: API and CLI acceptance passed; the operator wants to correct an input, run the existing EMA strategy and revisit readable results.
Intent: Complete the research task through visible controls and understand its experiment assumptions.
Interface: UI using actual computer-use browser tooling.
Setup: Candidate frontend targets the actual candidate backend, documented development auth bypass and matching dev API key configured; known fixture; no mocked backend. Operator already authenticated via the supported configured flow. Setup does not create this browser experiment.
Steps:
1. Open strategies/backtests, choose EMA and enter an invalid configuration/window. Observe the useful validation state; correct and submit the intended single-date AAPL experiment.
2. Follow visible queued/running/completed progress; open metrics, fills and full report. Observe engine/capital/leverage/fee/fill/return-basis labels, environment and relevant limitations.
3. Open the earlier saved baseline reference from history, then return to the new experiment.
Verification: Operator sees actionable correction text, the completed result and fills agree with real API/CLI meaning, can open the report, and is shown readable assumptions with legacy unknown values preserved. Relevant loading/error/empty states cannot imply success or realistic costs.
Persistence: Reload the new result and history and reopen its report; the same job, fill identities, metrics and meaning appear. Reload the saved baseline reference and compare original display.

### UC-V2-004: Research operator keeps diagnostic evidence separate — API, CLI, UI

Actor: Research operator comparing training-selected configurations before exploratory Discovery.
Scenario: Native backtests work; the operator runs a bounded EMA sweep and walk-forward over separate synthetic January windows.
Intent: Compare research evidence while preserving training selection and exploratory promotion restrictions.
Interface: API first, CLI readback second, UI third.
Setup: Prior backtest UCs passed; supported seed data spans required training/diagnostic windows; documented request schemas, strategy ID and known auth; no pre-created sweep/walk-forward/Discovery under test.
Steps:
1. Submit a small two-configuration sweep and minimal supported walk-forward via API; poll to terminal success and inspect training/diagnostic/latest-window evidence.
2. Use installed CLI research list/show to revisit both jobs. In browser open the same research jobs, inspect selection and use the supported exploratory Discovery action.
3. Attempt invalid/legacy evidence promotion using the public contract; observe actionable refusal, then use valid exploratory evidence through the intended correction path.
Verification: API response includes training-selected config and separate diagnostics/latest training provenance. stdout shows the same persisted selection. Browser operator sees exploratory scope, can revisit created Discovery evidence, and is shown refusal for invalid/legacy evidence. Owning diagnostic-invariance tests establish that changing diagnostics cannot select a winner; this journey proves product execution/persistence without claiming independent validation.
Persistence: Re-request jobs/Discovery, re-invoke CLI and reload browser views; identities and provenance remain unchanged.

### UC-V2-005: Operator can recover the preserved research baseline — API and CLI

Actor: Local research operator checking a reversible engine evaluation.
Scenario: Candidate runs exist in the isolated schema-compatible database and the original V1 image/catalog are preserved.
Intent: Recover compatible research execution and retain the original recorded experiments.
Interface: API and CLI, with browser saved-reference readback from UC003.
Setup: Documented controlled switch to immutable V1 image/source, original V1 catalog and same isolated compatible DB; original primary/Azure state untouched. No DB edits to alter jobs.
Steps:
1. Through API and CLI request original V1 saved results/trades/report and compare pre-switch public hashes/fields.
2. Submit a new baseline EMA fixture backtest through API, follow completion and installed CLI readback; then restore the candidate research environment through the documented lifecycle.
Verification: Client receives the original recorded V1 meanings and report, and stdout shows a completed new 1.223 run with compatible inputs. V2 records remain persisted without being reinterpreted or silently rewritten by V1. Candidate services return after the documented switch.
Persistence: Re-request baseline and candidate saved jobs after restoring candidate; retained IDs, recorded accounting and reports match their captures.
