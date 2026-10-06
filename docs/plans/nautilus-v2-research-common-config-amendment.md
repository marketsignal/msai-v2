# Bounded integration repair: common-runner engine identity

Actual public research sweep `7d14067d-fefd-4b15-86ec-cf755e831187` reached a
terminal response with both training trials failed and no selected config.
Native validation reported missing `instrument_id` and `bar_type`. The API
backtest preparation supplies those engine-owned fields, while research callers
correctly submit only operator parameters to the shared runner. This is FAIL_BUG
for the planned research journey; completed job status is not winner acceptance.

T4 repairs the owning common-runner preparation in `backtest_runner.py` and its
native regression tests. Derive required engine identity from actual run inputs
before the existing shared T2 validation helper. Preserve typed/unknown-field
refusal and do not overwrite inconsistent explicit identities. No research
selection, diagnostic policy, promotion, API orchestration, or database changes
are in scope. Existing API/CLI/browser and native economic/callback cases are
direct regressions; repeat the public sweep and walk-forward after rebuilding.

Preserve the failed sweep and captured public details. The original plan bytes
stay bound to their clean review; final paired reviews cover this concrete repair.

## Complete bar subscription identity

The first repair compared only `bar_type.instrument_id`. An actual native RC6
control subsequently proved that same-instrument 5-MINUTE, MID and INTERNAL
subscriptions against the minute-LAST-EXTERNAL catalog can return successful
results with two loaded bars but zero strategy callbacks and zero fills. This is
a second bounded T4 runner defect, not an accepted research outcome.

Compare the complete pinned native `BarType` with the run's canonical
1-MINUTE-LAST-EXTERNAL type. Use that same type in the native
`BacktestDataConfig.bar_types` filter so validation and emitted catalog data
share the contract. Preserve explicit values and refuse mismatches before native
execution. Do not introduce aggregation or additional bar-data support. Native
mismatch controls, a mixed-bar catalog control, and existing economics/callback
regressions belong to the owning runner tests; root repeats image/public gates.
