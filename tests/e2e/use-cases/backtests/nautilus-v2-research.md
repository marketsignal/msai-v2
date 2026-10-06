# Nautilus V2 research user journeys

Graduated on October 6, 2026 after fresh public API/CLI/research/recovery acceptance and
separate fresh actual-browser closure. The first verifier's PARTIAL browser-infrastructure
report is retained; its API/CLI/recovery coverage and the completed browser closure together
cover all five journeys. This is preliminary isolated research acceptance, not final candidate
certification, production installation, Entra, realistic costs, alpha or live-capital acceptance.

Surface coverage decision: API, CLI and UI are all required. Use only the sanctioned isolated
`msai-v2-research` stack, API `http://localhost:18800`, UI `http://localhost:13300`, documented
development key `msai-v2-research-local-only`, exact RC6 candidate and preserved V1/1.223 image.
The [runbook](../../../../docs/runbooks/nautilus-v2-research.md) owns fixture bootstrap, guards and
compatible recovery. Do not source root `.env`, enable vendor/broker services, intercept responses
or inject database/queue/Parquet product state. Execute acceptance through public responses,
installed CLI and actual browser only. Setup arranges the fixture; it does not create acceptance jobs.

Expected synthetic reference: AAPL.NASDAQ, January 2–2, 2025 UTC, EMA fast10/slow30/size1,
390 bars, 16 native fills, $1,000,000 opening USD capital and +$2.10 recorded realized gain;
return ratio `0.0000021`, display `+0.000210%`. New RC6 settings are leverage1,
FixedFeeModel/$0 commission, DefaultFillModel/seed42/configured random-slippage0. L1 bar liquidity
rules remain explicit; configured zero is not realistic-cost validation. UTC weekdays include
holidays and are not complete exchange sessions.

## UC-V2-001: Correct configuration and revisit a native experiment

Actor: Authenticated research operator.
Scenario: The existing EMA strategy must be validated, run and revisited on synthetic data.
Intent: Complete an iteration with useful errors and interpretable persisted evidence.
Interface: API.
Setup: Public strategy/schema discovery, sanctioned fixture and recorded original V1 job
`81766b6e-9da8-4a67-814b-ef7a73e8fb37`. Do not create candidate jobs in setup.
Steps:
1. Inspect EMA defaults; submit negative size, zero period and unknown field, observing useful 422 errors.
2. Correct configuration, submit January 2–2, poll to terminal, read result, all fill pages and report.
3. Compare recorded assumptions, independent Decimal fill cash flows and original saved V1 payload/report.
4. Submit unsupported daily bar identity and missing February input; observe actionable failed jobs without success metrics/reports.
Verification: The client receives field-specific configuration/date correction guidance, then can
use the newly created valid job ID to retrieve its completed experiment. The response includes
RC6, 390 bars, 16 fills, the reference economics, recorded assumptions and synthetic manifest.
Independent public fill arithmetic reconciles zero recorded fees, zero ending quantity and +$2.10
within floating-point precision. A follow-up request returns the same job/fills/report; original
saved V1 payload/report bytes remain unchanged. Unsupported daily identity and missing input error
bodies explain the failure, with no successful metrics or report.
Preliminary observation: Fresh verifier job `d5f55e29-42fd-4e0f-8602-446dc9cec90a` recorded
RC6/390bars/16fills/reference economics and synthetic provenance; independent arithmetic reconciled
zero fees and zero ending quantity. Original result/report bytes stayed unchanged.
Persistence: Re-request results/fills/report and history; compare again after actual application restart/recovery.

## UC-V2-002: Submit and revisit through installed CLI

Actor: CLI research operator.
Scenario: The same experiment is needed outside the dashboard.
Intent: Submit, correct and compare persistent results with the API.
Interface: CLI.
Setup: Installed candidate CLI with explicit isolated API URL/key; run outside the checkout so root `.env` is not loaded.
Steps:
1. List strategies, submit reversed dates and read actionable refusal.
2. Correct to January 2–2 and submit an independent CLI experiment.
3. Read show/trades/history and download its report, then compare with API.
Verification: stderr explains the reversed-date error so the operator can correct the request.
stdout shows the newly created valid job ID, completed status, 390 bars, 16 fills, reference
economics and actual assumptions. The next invocation shows the same saved experiment/fills;
the downloaded report matches public API content with CLI trailing-newline handling explicit.
Preliminary observation: Fresh CLI job `cb5c3740-681e-4e36-b5ca-e8abc9a5634c` completed with
390bars/16fills/reference economics and actual assumptions; CLI/API report comparison passed.
Persistence: Repeated CLI reads after candidate restoration retain the same job/fills/report; saved V1 assumptions remain unknown.

## UC-V2-003: Correct and revisit through the browser

Actor: Browser research operator.
Scenario: The operator needs useful form errors, readable financial assumptions and saved results.
Intent: Complete a bounded experiment and interpret it after reload.
Interface: UI.
Setup: Actual isolated frontend with supported local auth; no response interception or pre-created UI job.
Steps:
1. Open Run Backtest, select example.ema_cross/AAPL.NASDAQ, fast10/slow30; set both dates January 2 and read actual input values before submit.
2. Submit size−1, observe “Input should be greater than 0” and strategy validation guidance; correct size1 and submit.
3. Follow pending/history refresh to completion; inspect native metrics, all16 fill rows, Research simulation settings/limits and full report.
4. Reload, reopen through history, visit the saved V1 result with unknown engine/cost/fill assumptions and reopen the new result/report.
Verification: The operator sees useful negative-size validation, can correct the same form and
open its newly created experiment from history. The page reads 390 bars, 16 fills and +0.000210%,
with actual models/seed/probability, realized-balance meaning and synthetic limits. The operator
can open the full report with matching canonical metrics/assumptions, reload and reopen the same
saved job/fill rows/report through history. Legacy absent assumptions are shown as **Not recorded**;
new report rendering has no new console error.
Preliminary observation: Fresh browser closure job `c6fe273f-2478-43ad-a0fa-d91328876fc6` recorded
390bars/16fills/+0.000210% with actual assumptions and matching report/reload/history. New report
rendering had no new console error; the historical V1 report's original `save()` error was retained.
Observed selectors: Run Backtest dialog; Fast Ema Period, Slow Ema Period, Trade Size, Start Date,
End Date; Refresh history; backtest-accounting-scope; backtest-simulation-assumptions;
backtest-synthetic-scope; trade-log; Full report/iframe.
Persistence: Reload/history preserve job and fills; legacy absent assumptions display **Not recorded**.
Graduated regression: `frontend/tests/e2e/specs/nautilus-v2-research-real.spec.ts`, explicit opt-in only.

## UC-V2-004: Preserve training selection and exploratory Discovery

Actor: Research operator.
Scenario: A bounded sweep and chronological walk-forward must separate selection from diagnostics.
Intent: Retain training evidence and latest-window provenance without a validation/alpha claim.
Interface: API, CLI, UI.
Setup: Same sanctioned fixture and current public strategy; no research job created in setup.
Steps:
1. Run a grid sweep January2–9 with objective `total_return`, fast EMA choices `[10, 12]`, slow30/size1, holdout_days2, purge_days0, min_trades1, require_positive_returnfalse and max_parallelism1; inspect training versus holdout/full diagnostics.
2. Run rolling walk-forward January2–15 with the same grid/objective, train_days5, test_days2, step_days7, purge_days0 and no reserved holdout; inspect its two chronological windows and selected latest training window separately from test diagnostics.
3. Compare repeated CLI research show with API; in actual UI create exploratory Discovery and inspect/reload provenance.
4. Attempt failed-sweep promotion, observe disabled UI/refusal and API409 without candidate creation.
Verification: API responses include the newly created sweep/walk-forward IDs, successful trials,
training-selected config and separate diagnostics. For the explicit `total_return` requests above,
the sweep response includes fast12/training index0/January2–7, and walk-forward includes
fast10/latest chronological index1/January9–13 with January14–15 test diagnostics. CLI stdout shows
the same selection and provenance; the next invocation returns the same research job. The operator
sees training/exploratory labels and separated diagnostics, can create and open Discovery from
eligible training evidence, and sees matching provenance after reload/reopen. An ineligible failed
sweep shows disabled Discovery/refusal; the API error body explains promotion refusal without
creating a candidate. Different objectives may select different winners and must not be compared
against these `total_return` expectations.
Preliminary observation: Fresh sweep `305a8040-db51-4c64-acb5-a7518c1ba739` selected
fast12/training index0/January2–7. Fresh walk-forward `ec0a0fd7-b05d-4343-98a0-0c1ba8226949`
selected fast10/latest index1/January9–13 with January14–15 diagnostics. Fresh browser Discovery
creations retained exploratory provenance; the original failed sweep stayed refused. These
total-return winners differ from the earlier root Sharpe journey without changing selection meaning.
Persistence: API/CLI research show and browser job/Discovery reload/reopen preserve selection and provenance.

## UC-V2-005: Recover compatible V1 code and return to RC6

Actor: Research operator testing recoverability.
Scenario: Candidate execution must preserve the original experiment and usable prior engine.
Intent: Recover actual application behavior against preserved compatible state.
Interface: API, CLI, UI readback; documented local operational switch.
Setup: Preserve immutable V1 image/catalog/public hashes; inspect all public job/status pages and refuse switching while work/deployments are active.
Steps:
1. Switch only the five isolated app services to the preserved V1 image against the same compatible DB/data volumes.
2. Read original saved results/fills/report, submit a new baseline run and compare installed V1 CLI output/report.
3. Restore RC6 services, verify actual runtime/startup/refusal, then reread original and accepted candidate jobs.
Verification: The client receives a newly created completed V1 recovery job with 390 bars,
16 fills and +$2.10; follow-up requests return original saved payloads/fills/report unchanged.
CLI stdout shows both original and new recovery experiments and its downloaded original report
matches the preserved SHA256. After RC6 restoration, API responses include unchanged accepted
candidate results/fills/reports and useful unsupported-live refusal. The operator can open/reload
the original and RC6 saved results in the browser with their recorded meanings and unknown fields.
No dump restore, volume replacement or saved-record manipulation is used.
Preliminary observation: Actual V1 reruns completed390bars/16fills/+$2.10; original report SHA256
stayed `efb308bf37d58d4acbdc3849c8d89457b0e6faaaa0b0aaf96fbf8c7c532aa33c`.
Candidate results/fills/reports survived return unchanged; unsupported live operations still refused.
Persistence: Later API/CLI and actual browser reads retain original and RC6 job identities/semantics.
