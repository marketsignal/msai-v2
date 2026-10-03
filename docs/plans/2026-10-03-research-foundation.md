# Research foundation: accounting and usable verification

Status: diagnosis complete; broad plan review received and named findings incorporated; awaiting closure review before production edits.

Workflow: `fix-bug research-foundation` in `fix/research-foundation`.
Immutable base: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`.
Worktree: `/Users/pablomarin/.codex/worktrees/research-foundation/msai-v2`.
User authority: the operator approved executing the Master Plan on October 3, 2026. This is its first bounded repair batch, not completion of Milestone 1 or authorization to release/trade.

## Outcome and boundary

An operator can run the existing equity reference strategy, see consistent return ratios and first-day results, inspect actual fills and engine-recorded fees, and use the CLI and browser verification harness without the reproduced startup failures. Preserve the existing architecture. No DB migration, vendor download, broker connection, live order, Azure mutation, push or release belongs to this batch.

This batch repairs measured accounting defects; it does not certify investment research. Nautilus account balance reports are not a complete marked-to-market NAV series. Full market/session semantics, realistic cost modeling, leakage/holdout controls and portfolio deployment remain their existing Master Plan gates. Expose the balance-series and cost limitations where users interpret these results. Do not silently backfill or relabel existing stored reports as corrected.

## Reproduction and root causes

1. Installed Nautilus 1.223.0 reports a balance change 1000→1100 as `PnL% (total)=10`. `_extract_metrics` copies that number into the ratio-valued API field `total_return`; the UI multiplies by 100 again. The recorded real AAPL result likewise differs by 100× from fill-price P&L divided by the engine's starting capital.
2. `_compact_account_report` applies daily `pct_change().fillna(0)`, discarding the first session's movement from opening capital. A one-day 100→110 account produces return 0. Grouping asynchronous account reports by timestamp and summing only present rows invents jumps when another account has not updated at that instant.
3. The shared equity/drawdown calculation seeds its running peak from the first post-return observation. Returns `[-0.1, 0]` therefore report zero drawdown instead of −10%. The backtest worker rebuilds equity on a default 100,000 rather than the engine's actual capital basis (1,000,000 per configured venue).
4. Execution persistence uses every order report row, prefers requested quantity to filled quantity, and sets commission and absent P&L to zero. A canceled unfilled order becomes a trade; a partially filled order can claim its full requested size. Installed Nautilus exposes an individual `generate_fills_report` containing execution quantities, prices, timestamps and fees.
5. Eager CLI import constructs Pydantic Settings against cwd `.env` with default `extra=forbid`. Valid Compose-only keys fail; displayed validation errors echo their values. Synthetic subprocess reproduction distinguishes successful empty dotenv from failing Compose extras/invalid known fields without reading real secrets.
6. `pnpm dev -- --port 3300` forwards the literal separator to Next. The harness additionally points browser navigation at an overridden base URL while still managing a hardcoded local server.

Local browser history was observed failing intermittently and then succeeding after reload on the same source. Direct authenticated history requests and their CORS headers succeeded. This is not yet a proven application root cause; do not patch transport speculatively. The complete candidate journey must be rerun and any remaining failure diagnosed rather than hidden.

Existing source/runtime evidence: `docs/audits/2026-10-03/runtime-journey.md`, `runtime-assessment.md`, `research-data.md`, plus the recorded local/Azure response artifacts. Reproductions use installed library contracts; current official documentation supplements them: [Nautilus analysis API](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/api_reference/analysis.md), [Pydantic dotenv](https://docs.pydantic.dev/latest/concepts/pydantic_settings/#dotenv-env-support), [error-display configuration](https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.hide_input_in_errors), [structured error inputs](https://docs.pydantic.dev/latest/api/pydantic_core/#pydantic_core.ValidationError.errors), [pnpm 10 argument forwarding](https://pnpm.io/10.x/cli/run). Installed versions are Nautilus 1.223.0, Pydantic 2.12.5 and pydantic-settings 2.13.1; no dependency upgrade is needed.

## Minimal production changes and ownership

### A. Account returns, metrics and execution records

Producer owns `backend/src/msai/services/nautilus/backtest_runner.py`, `backend/src/msai/services/analytics_math.py`, `backend/src/msai/workers/backtest_job.py`, and their focused accounting/worker tests. Coordinate schema/API/consumer changes with task B before editing shared contracts.

- Keep public return/drawdown fields as fractional ratios. Convert percentage-valued Nautilus keys explicitly; do not divide already-ratio fallback fields. A genuine zero must not trigger fallback. When a complete account-balance path exists, derive headline total return, drawdown and daily-risk statistics from the same opening-capital and daily-return basis as charts/report. Do not substitute per-trade notional return for account return when account capital is absent, or position-return Sharpe for a daily-balance Sharpe claim.
- Preserve the known initial balance for each venue/account before daily compaction. Carry forward each account's last known state across asynchronous updates and sum the resulting aligned states. Do not sum different currencies without conversion or treat missing opening states as zero; reject unsupported/ambiguous accounting inputs explicitly. Do not introduce an FX engine.
- Include first-session performance against initial capital and seed drawdown at initial equity. Thread actual starting capital through series materialization. Retain a clear legacy/unknown-capital boundary for stored results.
- Generate and transport individual execution fills separately from order reports. Persist each true fill once with its filled quantity, execution price/time and actual commission in the supported currency. Do not persist unfilled orders. Missing per-fill realized P&L is null, not invented zero; do not build a new lot-accounting engine in this repair.
- Define existing `num_trades` as executed-fill count, expose `num_fills` explicitly, and label this value Fills for versioned results. This intentionally corrects the current order-row count: an unfilled/canceled order no longer qualifies toward a minimum and individual partial fills count individually. Inspect direct portfolio counting consumers and stop counting unfilled order intents there too, while preserving `orders_df` for order-intent parity. Keep the existing threshold values and test the corrected eligibility input; thresholds are fill thresholds, not independent closed outcomes. Document this limitation under the still-open selection findings. Closed-position counts, if exposed, are a separate named metric.
- Keep costs honest: preserve actual engine-recorded commissions, including known zero, and reject unsupported currency/malformed fill economics rather than silently losing them. This does not establish a realistic broker fee/slippage model.

### B. Accounting contract and presentation

Producer owns `backend/src/msai/schemas/backtest.py`, the results/trades mapping in `backend/src/msai/api/backtests.py`, `frontend/src/lib/api.ts`, backtest result/chart/trade components and their owning tests. Coordinate with A on additive metadata and existing JSON storage; no migration.

- Pass through nullable unknown fill P&L/commission honestly and render unavailable values clearly. Preserve known zeros only for versioned actual-fill results. Keep quantity, price, fee and outcome labels distinct.
- Use one optional accounting metadata contract: `{version: 1, basis: "realized_account_balance", initial_capital: positive number, currency: "USD", costs: "engine_recorded"}`. Persist it in existing metrics/series JSON, expose it at the top-level results and trades responses, and keep the numeric runner metrics separate if needed by numeric-only consumers. Top-level result metadata must survive series materialization failure. No metadata means legacy/unknown; do not infer corrected accounting from old series presence.
- Correct the native chart's cumulative-return baseline: for versioned results use `accounting.initial_capital`, never the first post-return daily equity. A 100→90 first session must plot −10%, not 0%; a first-day gain must likewise appear. Keep legacy chart behavior visibly identified as legacy with unknown opening capital. Add a focused transformation/component regression independent of tiny rounded reference values, plus actual-browser acceptance.
- The trades endpoint must inspect the parent backtest's accounting version. Unversioned rows were saved from order reports with placeholder `pnl=0` and `commission=0`: return these economics as null/unknown, expose legacy status through absent accounting metadata, and label these rows/counts as legacy order records rather than certified fills. Do not rewrite old DB rows. Versioned fill rows preserve actual zero commission and nullable unknown P&L. Test both legacy stored zeros and new genuine zero/nonzero values through the API and presentation.
- Display the balance-return limitation and engine-cost scope in the result/report context where users can make a meaningful research decision. Keep wording plain; no infrastructure details in the user flow. Verify API/CLI consumers tolerate additive metadata/nullability and correct counts.
- No visual redesign or new workflow modes. Existing fonts, controls and account semantics remain.

### C. CLI and browser harness

Producer owns `backend/src/msai/core/config.py`, focused Settings/CLI startup tests, `frontend/playwright.config.ts`, focused harness checks and stale harness instructions in directly affected E2E specs.

- Use `extra='ignore'` for the shared dotenv contract and `hide_input_in_errors=True`. Retain strict validation of known fields and production secret checks. Do not serialize validation inputs via `.errors()`/`.json()` or dump settings. Test fresh CLI startup from synthetic root/backend directories with secret sentinels.
- Correct local startup to `pnpm dev --port 3300`. Default target launches/reuses local port 3300; an explicitly supplied `PLAYWRIGHT_BASE_URL` targets an already-running environment and does not start an unrelated local server. Do not turn arbitrary URL text into shell commands. Preserve authentication boundaries.
- Prove the default server launches from a fresh state and the explicit target skips server management. `--list` and reuse of the already-running frontend alone do not prove startup.

Main agent owns plan, reference reconciliation, runtime setup, Master Map/Plan progress, changelog/solution documentation, and integration. Every worker is in a shared worktree; preserve others' edits and use explicit file ownership. A and C can proceed independently after plan approval; B consumes A's agreed contract.

## Regression tests: RED before GREEN

- Actual Nautilus percentage statistic versus a hand-calculated ratio, with an already-ratio fallback control.
- One-day gain/loss, initial loss and zero-change controls, multiple same-day snapshots, non-simultaneous account updates, missing/unsupported currency/opening data.
- Independent decimal reconciliation of starting capital, ending balance, daily compounding and monthly return; chart/report metrics share the same inputs. First negative period establishes drawdown.
- Individual partial fills versus one requested order, canceled/unfilled exclusion, nonzero/zero commissions, unavailable P&L, stable fill identity and explicit fill-count semantics. Preserve the order-intent parity consumer and exercise the direct portfolio count change. Use installed Nautilus report fixtures or actual engine report generation, not guessed field names.
- Nullable/legacy result serialization and presentation, with known zero distinguished from unknown.
- First-day chart gain/loss against opening capital; legacy order-row economics remain unknown even when stored as zero; metadata is available if chart series failed.
- Fresh subprocess CLI import/help with synthetic Compose extras; invalid known field still fails without sentinel exposure; production validators remain active.
- Default/explicit-target browser config behavior and actual default fresh-server startup.

Run focused owning tests, lint/type checks for changed paths and the applicable frontend build. Include directly affected portfolio computation/objective tests, the corrected eligibility-count input, and first-negative-period HTML report drawdown. Do not launch the full unrelated backend suite. Existing baseline failures remain named; final verification is bound to the actual candidate.

### Advisory boundaries from the broad review

The review's five P3 notes are not closure blockers. They clarify remaining research validity limits rather than expanding this batch into selection-policy or market-calendar redesign. Daily risk statistics must retain an explicit observation/sample basis; add a flat-gap control and do not describe this as certified exchange-session annualization. Existing numeric undefined-statistic sentinels remain backward compatible (for example zero for insufficient observations); the API acceptance claim about unknowns applies to fill economics and capital metadata, not a promise that every research metric has become nullable. Nullability changes across all research objectives are deferred. Unversioned historical research trials, reused Optuna journals and graduation candidates must not be compared or promoted as equivalent to corrected results; selection/provenance findings remain open. Do not reuse an old optimization study as acceptance evidence. A full NAV series, market calendars, cost calibration and selection validation remain separate Master Plan gates.

## User journeys and surface coverage

All three surfaces are **Covered**. API→CLI→UI order applies. Final UI acceptance uses computer-use tooling in a real browser with actual backend/data, never mocked network responses. Component-level negative/legacy tests may use mocks and do not replace the real journey.

### RF-API — Reconcile a strategy run

- **Actor:** research operator preparing to compare strategy versions.
- **Scenario:** existing AAPL minute data and registered EMA code are available in the guarded local environment; the operator needs to know what the simulation actually earned before evaluating the idea.
- **Interface:** API.
- **Intent:** run a reference experiment and retrieve a consistent, interpretable result with its executions.
- **Setup:** documented development authentication, existing data, strategy discovery/validation and idle research capacity through supported APIs. No direct DB writes or new data download. Broker/vendor access remains disabled.
- **Steps:** discover the strategy/data; submit a new bounded backtest with the recorded reference configuration; poll to completion; fetch results, all fill pages and report; reconcile fill-price P&L/recorded fees against starting/ending balance for the flat-ending reference case.
- **Verification:** response includes the completed run, consistent ratio/daily/monthly values, actual fills and explicit accounting scope. The client can open the report and identify unsupported/unknown values without treating them as zero. Net fill totals agree within specified rounding tolerance for the reference's supported assumptions.
- **Persistence:** re-request results/history/fills and receive the same run and economics.

### RF-CLI — Start reliably and inspect the experiment

- **Actor:** strategy author working from the repository root and backend directory.
- **Scenario:** shared Compose configuration previously prevented CLI startup; the author needs to submit/retrieve experiments without navigating around import errors.
- **Interface:** CLI.
- **Intent:** use the documented command interface to run and revisit a bounded strategy experiment.
- **Setup:** documented process-environment API target/key; synthetic cwd tests prove root config behavior without emitting real secrets. Existing strategy/data via sanctioned discovery.
- **Steps:** invoke help from the supported root setup; submit a reference backtest through its CLI command; inspect status/results; compare its contract and economics with the API reference. Use synthetic invalid configuration for the negative startup case.
- **Verification:** stdout names the run/status/results, subsequent commands retrieve it, and invalid configuration explains the field problem without printing sentinels. Errors do not masquerade as success.
- **Persistence:** a new CLI invocation retrieves the same completed run.

### RF-UI — Run, understand and revisit a backtest

- **Actor:** signed-in or documented local-development research operator.
- **Scenario:** the operator wants to evaluate an EMA version through the dashboard and distinguish filled executions from trading outcomes and unrealized performance.
- **Interface:** UI.
- **Intent:** submit an experiment, understand its measured result and return to it later.
- **Setup:** already-serving candidate API/workers with existing data and documented dev authentication; no mocks. Do not infer Entra permission proof from local bypass.
- **Steps:** open Backtests; submit the reference experiment; observe progress/completion; open results/charts/fill log/report; inspect capital, units, counts, costs and balance-series limitations; navigate away and revisit. Exercise the existing missing-data/invalid-input recovery path with ingestion disabled, and complete a valid action afterward.
- **Verification:** the operator sees readable, consistent values and distinct unknowns; actions and errors explain the next step; the reference results agree with the independent API reconciliation. Verify affected account/environment labels and no broker execution is initiated.
- **Persistence:** reload/revisit the completed run and observe the same result and fills.

The harness additionally starts a fresh local frontend and reaches a real page before cleanup; an explicit existing target runs without managing a local server. Browser failures remain real blockers for UI acceptance, even when API/unit checks pass.

## Completion and next batch

Obtain clean plan evidence, then producer RED/GREEN evidence, preliminary E2E, solution/changelog updates, bounded simplification, final immutable-candidate paired review and verifier evidence. Do not commit/push/release merely to make progress; keep any separately required shipping authorization explicit.

Update the Master Map by subfinding, not by closing all M05/M19/M14 after this batch. Milestone 1 remains in progress until the broader data/validation acceptance passes. Next operational batch: fail-closed exact-revision CI gating, owned-orphan NSG cleanup and stopping-deployment release refusal before publishing to Azure; supervisor identity/compatibility and restore/storage work retain their own operational proof.
