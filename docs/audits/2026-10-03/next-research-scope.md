# Next research batch: source ownership and acceptance starting point

Read-only preparation on October 3 local / October 4 UTC, while merged-main
deployment verification runs. Source: `be26c9014086bb79863b662a5539683ee1f20d37`,
tree `637234904cc43c8f85ba3c8ab663e27202da27b5`, identical to merged main
`fa4c8f8c5814e3a33dac8bb0bd56ae7b51927ca7`. Independent explorer inspected
source/tests; no tests, cloud requests or implementation changes were performed
for this mapping. This is preparation, not an approved implementation design.

## Existing owners

Paths below are repository-relative at that source revision.

| Contract | Existing path and gap |
| --- | --- |
| Research inputs | `backend/src/msai/schemas/research.py:12`, `schemas/backtest.py:16`, `api/research.py:379`: requests lack explicit dataset/schema/session/capital/cost contracts. Standalone backtests resolve instruments in `api/backtests.py:352`; research forwards raw names in `workers/research_job.py:154`. |
| Execution assumptions | `services/nautilus/backtest_runner.py:413` owns execution. Around lines 442–460, venues use hardcoded starting capital and date-only bounds; the boundary does not expose a commission/slippage/spread/latency configuration. `catalog_builder.py:50` fixes one-minute bars. These are M03/M15/M19 touchpoints. |
| Data sample | `services/parquet_store.py:264` namespaces asset/symbol/month; line 110 read–merge–replace is not serialized between writers. `services/nautilus/catalog_builder.py:157` fingerprints metadata before shared rebuild. `services/symbol_onboarding/coverage.py:238` cannot detect internal gaps from partition endpoints; recent-gap tolerance at line 278 is an inventory policy, not exact research sample proof. |
| Standalone lineage | `models/backtest.py:51,105` holds source/config/engine/data provenance. `api/backtests.py:320` hashes the strategy file at enqueue; `workers/backtest_job.py:379` later executes its mutable path. `services/nautilus/catalog_builder.py:463,490` describes file metadata without enforcing the selected date window. Registry hashing at `services/strategy_registry.py:676` includes sibling config, but is not the hash used by the backtest endpoint. |
| Research lineage | `models/research_job.py:34`, `models/research_trial.py:38` already store JSON and trial links. `services/research_engine.py:936` calls the runner directly; `workers/research_job.py:395` creates trials without backtest links. Use these existing records rather than adding another orchestration service. |
| Study reuse | `services/research_engine.py:403,1211` derives/reopens an Optuna study without immutable strategy/helper/data/cost/split identity. Reuse must not mix materially different experiments. |
| Selection vs final test | `services/research_engine.py:668–717` ranks configurations using holdout results. Optuna feeds them into optimization at 1283–1305. The fallback at 1386 omits holdout-error handling. These samples currently serve selection/validation, not an untouched final test. |
| Walk-forward finalization | Although engine train/test windows are separate, `workers/research_job.py:372–390` selects the best test window for `job.best_config`. Fixing only engine selection would leave this additional source of bias. |
| Promotion | `api/research.py:264–293` promotes completed jobs with results, and explicit trial selection bypasses trial-success checks. `services/graduation.py:79,157` checks existence/safety and allowed transitions, without establishing quantitative final-test validity. Discovery should stay visibly exploratory. |

All abbreviated backend paths after the first row start under `backend/src/msai/`.
Findings refine the already-open research, data and provenance issues; they do
not establish new runtime failures or close any Master Map finding.

## Smallest coherent starting slice

Use one existing equity and one-minute bars, a declared session/date window,
an explicit supported cost assumption, and a small grid experiment:

1. Resolve/persist the strategy package, configuration, exact data sample,
   engine identity, capital/cost assumptions and split boundaries. Execute
   preserved inputs or refuse detected drift; a metadata fingerprint alone
   cannot establish reproducibility.
2. Prove date/session bounds and a declared missing-data policy, including
   internal gaps. Refuse unsupported asset/interval/cost requests clearly.
3. Select on training/validation only, freeze the chosen configuration, then
   perform a separately recorded final evaluation. Changing final-test values
   must not change the selected configuration. Include worker finalization,
   not only engine ranking, in that check.
4. Distinguish exploratory completion from successful validation. Failed,
   absent or reused final evidence must not silently permit a validated claim.
5. Reproduce the evidence via API and CLI, then submit/inspect/reload and
   exercise unavailable-data recovery through real browser computer use.

This is a milestone-1 starting point. It does not certify futures, portfolio
out-of-sample weighting, multi-account deployment, alpha or all provenance.

## Verification and interface owners

Focused existing tests: `backend/tests/unit/test_nautilus_backtest_runner.py`,
`test_data_lineage.py`, `test_catalog_builder_streaming.py`,
`services/nautilus/test_catalog_builder.py`, `test_parquet_store.py`,
`test_research_engine.py`, `test_research_api.py`, `test_graduation.py`, and
`backend/tests/integration/test_research_flow.py`. Existing mocked engine and
metadata fixtures do not replace content-drift counterexamples, final-test
perturbation controls or real end-to-end execution.

CLI JSON submission already exists at `backend/src/msai/cli.py:2776,2793`.
`frontend/src/components/research/launch-form.tsx:115` omits the relevant
holdout/filter/assumption controls; `frontend/src/app/research/[id]/page.tsx:224`
offers promotion for completed jobs. UI evidence and eligibility should match
the API while keeping the research workflow understandable.

Before implementation, settle the exact equity session/timezone, missing-bar
policy, supported cost approximation, strategy bundle boundary, legacy-result
treatment, final-test reuse rules, and walk-forward selection method. Resolve
these in the bounded task design using the existing product goal and data;
do not infer that this preparatory document authorizes a broad storage rewrite.
