# Training selection and exploratory discovery

This repair separates parameter selection from reserved-period diagnostics. Previously, holdout metrics replaced training metrics before ranking and Optuna feedback; walk-forward finalization chose the window with the best test score. Changing only those diagnostic values could change the configuration saved for onward research.

New sweeps rank complete, successful, eligible training results using finite objectives. Minimum-trade and positive-return requirements apply to all search modes. Ties preserve candidate order. The eligible winner is frozen before its optional holdout run, and only that winner receives the holdout diagnostic. A diagnostic error does not trigger reselection. Optuna receives training feedback; filter rejections are pruned and unusable executions fail. A fresh study namespace prevents historical holdout-driven feedback from contaminating a new run.

Walk-forward discovery uses the latest chronological window's eligible training winner. Missing eligible training in that window produces no automatic choice; there is no silent fallback to an earlier window. An operator can explicitly retain an earlier eligible training choice as exploratory discovery. Test metrics, test errors, nested training splits and full-period replays remain separately visible diagnostics.

The persisted report carries version 1 selection provenance. Public job reads expose its basis, exploratory scope and discovery eligibility or refusal reason. Legacy results remain readable, with unknown selection semantics; creating a new discovery candidate from them requires rerunning research. Invalid, incomplete, pruned or failed training evidence cannot create a candidate. Numerical training metrics remain flat in the candidate; provenance is stored only under `metrics.selection`, outside executable strategy configuration. Manual choices carry the distinct `explicit_trial` policy.

The dashboard explains training selection, resolved holdout dates and diagnostic states. Its primary action is **Create Discovery Candidate**. Graduation presents selection provenance separately from numerical metrics. Explicit holdout/purge requests that leave no training range are refused. Existing automatic 20% splits for ranges of at least 252 days are retained and disclosed.

Research submission errors show the server's corrective message and recognized field labels without echoing the submitted configuration or validation context. Real browser testing exposed an additional sizing defect with populated JSON inputs: content-sized textareas expanded the dialog's implicit grid column. A single constrained grid column on this form keeps its fields and message inside the dialog. Owning regressions use actual strategy JSON and check the entire dialog and control bounds at phone and desktop widths; a fitting alert alone did not catch the defect.

## Verification boundary

Owning regression tests exercise reserved-value counterfactuals, actual Optuna storage/feedback, eligibility and failure controls, worker finalization, API refusal and CLI parity. Final candidate runtime acceptance and independent code/verifier receipts are still required; this document is not a completion certificate.

This repair concerns research sweep/walk-forward selection only. Portfolio out-of-sample allocation selection, independent graduation evidence gates, data completeness, costs, immutable strategy/data bundles and final-test reuse controls remain open. A short real-data journey proves workflow behavior, not reliable alpha, portfolio validity or live-capital readiness. No migration or historical-result rewrite is required.
