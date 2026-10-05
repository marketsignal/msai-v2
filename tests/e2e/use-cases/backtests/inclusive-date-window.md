# Inclusive date-window user journeys

Graduated after actual local API, CLI, research and native computer-use acceptance on October 5, 2026. The guarded local fixture and boundaries below are required; this is not Azure or live-trading certification.

Surface coverage decision: API, CLI and UI are all required. Existing guarded local research runtime, API http://localhost:8800 and browser http://localhost:3300. Supported development authentication uses the documented msai-dev-key; it does not certify Entra login. Existing registered EMA strategy19131631-1d46-4095-ab0b-8702aabac9d1 and licensed AAPL.NASDAQ minute data. No mocked responses, direct DB/queue/file setup, ingest, credentials or broker activity. Reuse existing public catalog/strategy listings for setup. Do not read application source to execute verification.

Known public acceptance reference: December3,2024 EMA fast5/slow20,size1 should report502 actual bars,32fills,+$1.13 simulated cash profit with engine-recorded commissions0.00USD and opening capital1000000USD. Native consumed timestamps and exact next-midnight exclusion are proved separately by owning real-engine tests. E2E observations must use public responses, CLI output and visible browser content, not logs or database assertions. Economics are not alpha/cost validation.

## UC-IDW-API — complete a one-day backtest and correct a reversed request

Actor: Research operator creating a strategy experiment through the public API.
Scenario: Existing AAPL data is available and the operator wants a complete single-day simulation, then needs to correct an accidentally reversed range.
Intent: Trust that the selected calendar day was processed and retrieve the same saved result later.
Interface: API.
Setup: Supported dev authentication and public strategy/instrument availability; no job created in setup. Record history total and existing reference results29e190ef-8b42-4115-90bc-34c100742597 before steps. Root retained the pre-handoff public response: canonical JSON (sorted keys, compact separators) SHA2568cabbcfac755d1d5667a23bfa9179a120db72a0287a145892645c564914cfd59. Compare later public responses against that exact semantic snapshot; HTTP whitespace/key-order variation is not a stored-result change.
Steps:
1. Submit December3–3 EMA5/20,size1 through public backtest run; poll the returned ID until terminal and read its results, trades, status and report availability.
2. Submit December4–3 with the same configuration, observe correction guidance, and compare history to confirm no invalid job was created.
3. Re-request the valid saved ID and its result, preserving its requested dates.
Verification: Client receives completed results with num_bars502,32fills and ready series; fill dates areDecember3, cash-flow reconciliation matches+$1.13 and reported ratio against1000000 opening capital. Error body explains end date must be on or after start date; history has only the valid newly created job. Follow-up request returns the same identity/dates/count/economics. The old100-fill saved result remains byte-for-byte identical when compared through public JSON responses.
Persistence: Fetch valid status/results/trades again in a later request and confirm stable dates/count/fills/economics.

## UC-IDW-CLI — request and revisit the same inclusive day

Actor: Research operator submitting strategy experiments from the supported Python CLI.
Scenario: The operator works from a shell and needs the same calendar meaning and saved result as the dashboard.
Intent: Complete a single-day experiment, understand a range error and revisit results without infrastructure knowledge.
Interface: CLI.
Setup: Intended local API URL and supported development key; use actual python -m msai.cli commands in the guarded backend runtime. No submission during setup.
Steps:
1. Read backtest run help and submit the same December3–3 EMA5/20,size1 request through backtest run; poll with backtest show until terminal.
2. Invoke the same command with December4–3 and read the validation guidance; a subsequent history invocation confirms no invalid job was added.
3. Reinvoke backtest show on the successful ID.
Verification: stdout shows inclusive UTC help, completion, num_bars502,32fills and matching requested dates/economics. stderr/stdout explains the reversed-range correction rather than an opaque engine failure. The next show invocation retains the same completed result.
Persistence: Separate show and history invocations retain the ID, dates, count and economics.

## UC-IDW-UI — correct the form and understand a saved day

Actor: Research operator using the real local Backtests dashboard.
Scenario: The operator opens the backtest form, accidentally reverses the dates, corrects them and wants to inspect and later reopen the actual saved experiment.
Intent: Complete a day of strategy research with understandable date meaning, errors and trustworthy result visibility.
Interface: UI.
Setup: Existing guarded candidate-mounted services and supported development authentication. Navigate through the actual browser using computer-use tools. No response interception or API substitution for form submission.
Steps:
1. Open Backtests and its run form, select the existing EMA strategy/AAPL instrument, EMA5/20,size1 and December4–3. Read the nearby inclusive UTC explanation; submit and observe readable end/start correction guidance on the form.
2. Correct both dates toDecember3, submit through the form, follow actual progress and open the completed results.
3. Read Bars processed502, Fills32, account balance return, trades and full report; reload the result and reopen it through Backtests history.
4. Open old reference29e190ef and read its preserved100fill result with Bars processed Not recorded, then return to the new result.
Verification: Operator sees a specific date correction, can correct the same form, and sees the resulting saved32fill simulation with502bars andDecember3 dates. Results/report/history remain usable and persistence keeps the same count/economics. Old rows show Not recorded rather than fabricated zero. Capture observed locators/text and native Cua screenshot output where useful; cite a saved screenshot path only when a file was actually saved.
Persistence: Reload and history re-open retain the new job and its date/count/fill/economic observations.

## UC-IDW-RESEARCH — retain training selection across inclusive windows

Actor: Research operator running bounded strategy validation through the public API.
Scenario: Existing December2–5 AAPL data is available; the operator wants training choices separated from a reserved-period diagnostic and a later test window.
Intent: Understand which calendar days each research stage used and retain an exploratory training choice.
Interface: API.
Setup: Supported dev authentication and refreshed public strategy/data availability, no research submission in setup. Independent actual-engine reference counts: December2=478, December3=502, December4=477, December5=471; December2–3=980 and fullDecember2–5=1928. These are supplied expected values, not E2E log assertions.
Steps:
1. Submit a two-choice fast EMA5/10,slow20,size1 sweep forDecember2–5 with holdout_days1,purge_days1,min_trades1,require_positive_returnfalse and bounded parallelism1. Poll to terminal and inspect job/trials, trainingDecember2–3, purgeDecember4, diagnosticDecember5 and native counts in public metrics.
2. Submit one rolling walk-forward window forDecember2–5 with train_days3,test_days1,step_days3,holdout_days1,purge_days1 and the same grid. Inspect trainingDecember2, purgeDecember3, diagnosticDecember4, testDecember5 and publicly returned native counts.
3. Create Discovery for the eligible training winner, or inspect the concrete eligibility refusal if actual strategy evidence is ineligible. Re-request job and candidate if created.
Verification: Response includes logical inclusive window boundaries and native counts agreeing with independent exact-window controls. Sweep training uses980bars, reservedDecember5 uses471 and full replay1928. Walk-forward one-day training uses478, reservedDecember4 uses477 and testDecember5 uses471. Research selection provenance remains training; holdout/test metrics are separately labelled diagnostics. Discovery, if eligible, uses training config/evidence, not diagnostic ranking. A tiny experiment proves workflow meaning, not statistical validation.
Persistence: Later job detail/history and candidate detail preserve selection, boundaries, count/evidence and Discovery identity or refusal.
