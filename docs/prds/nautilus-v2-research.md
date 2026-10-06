# PRD: Nautilus V2 research integration

**Version:** 1.0
**Status:** Approved by the user on 2026-10-06
**Author:** Codex, from the user's agreed master-plan scope
**Created / updated:** 2026-10-06

## 1. Overview

Enable the existing MSAI research operator to complete one real strategy iteration journey on Nautilus V2: discover and validate a Python strategy, run a bounded experiment, inspect reconciled results, and revisit them through API, CLI and browser. The completed native trial establishes partial feasibility and known failures; this slice must establish actual application behavior while preserving the accepted 1.223 release and saved evidence.

## 2. Goals and success metrics

| Outcome | Required proof |
| --- | --- |
| Real application strategy journey | One representative existing strategy completes discovery/validation, backtest, results/trades/report and reload through API, CLI and actual browser using real backend responses. |
| Reconciled economics and behavior | One independently calculated tiny reference agrees on fills, fees, ending balance and account-return basis. Representative strategy callbacks, indicators, signals and order/fill timelines are explained against the baseline; P&L similarity alone does not pass. |
| Persistence and compatibility | Saved reference results retain their recorded meaning and availability; candidate data/state cannot overwrite the baseline's only copy. |
| Useful failure recovery | At least one supported invalid-input or unavailable-input case produces an actionable failure/refusal, followed by a successful correction without hidden state edits. |
| Honest research boundaries | Training-only selection and exploratory Discovery provenance/refusal remain correct in affected paths; the trial's unresolved funding and execution cases retain explicit evidence. |
| Coherent application | Every service consuming the changed integration starts in the intended candidate environment or reports an explicit supported limitation. Changed dependencies cannot silently disable live routes or safeguards. |

### Non-goals

- Production/Azure installation, live orders or investment approval.
- Complete live V2 lifecycle, cache recovery, projection and broker migration.
- Shared-capital portfolio certification, two-account live acceptance or repair of all risk/accounting findings.
- Certification of complete market sessions, realistic broker costs, immutable experiment inputs or independent final-validation/graduation.
- A general dual-engine framework, broad storage migration, unrelated dependency upgrades or speed benchmarks.

## 3. Persona and access

The existing authenticated MSAI research operator uses the established API, CLI and browser controls. Preserve supported authentication and visibility behavior. This slice creates no new persona, permission grant, public tenant or trading authority.

## 4. User stories

### US-001: Discover and validate a real strategy

**As a** research operator, **I want** a representative existing Python strategy and its configuration to work on V2, **so that** I can continue strategy iteration through familiar controls.

```gherkin
Given a representative existing strategy and its declared configuration
When I discover it and submit a supported configuration for validation
Then the application identifies the strategy and accepts valid inputs
And invalid inputs explain what to correct before execution
```

Acceptance: actual strategy/config discovery and validation succeed; unsupported configuration has a useful refusal; strategy behavior changes are explained by observed callbacks, indicators, signals and first-trade/order/fill timing.

| Condition | Expected outcome |
| --- | --- |
| Invalid or unsupported configuration | Actionable refusal rather than a successful empty result. |
| V2 indicator/warm-up changes | Visible experiment interpretation retains the chosen engine and explained behavioral differences. |

**Priority:** Must have.

### US-002: Run and interpret a bounded native backtest

**As a** research operator, **I want** explicit inputs and reconcilable results, **so that** I can assess evidence without confusing engine execution with economic correctness.

```gherkin
Given known equity data, inclusive dates and explicit economic assumptions
When I execute the supported strategy backtest
Then its fills and account results reconcile with the expected reference
And the displayed return basis, costs and limitations are understandable
```

Acceptance: the tiny independent example and representative existing strategy both run; capital, currency, leverage, fees and fill assumptions are recorded/exposed where promised; inclusive/equal-date behavior remains correct; unknown legacy fields remain unavailable; actual native account/equity return meaning is reconciled before replacing analytics.

| Condition | Expected outcome |
| --- | --- |
| Missing required input or unsupported case | Useful refusal/failure; no financial-success claim. |
| Same-bar shared funding or unexplained one-tick difference | Retained failing/reproduced evidence, supported native resolution or explicit unsupported boundary; no portfolio certification. |

**Priority:** Must have.

### US-003: Follow and revisit the same experiment across interfaces

**As a** research operator, **I want** API, CLI and browser to agree on a persisted experiment, **so that** I can inspect trades, reports and history after completion.

```gherkin
Given a submitted real experiment
When I follow its progress and inspect results through API, CLI and browser
Then identifiers, status, units, dates, fills and financial meaning agree
And reopening the experiment after reload preserves its results and report
```

Acceptance: API-first, CLI-second and actual computer-use browser journeys cover submission, progress, result/trades/report, history/reload and meaningful failure/correction using real backend/data. Existing saved-result readback is preserved. Browser labels identify relevant scope, engine, assumptions and unknown values clearly.

| Condition | Expected outcome |
| --- | --- |
| Failed job or unavailable result | Understandable error and supported next action, with no misleading completed status. |
| Reload/history revisit | Same persisted experiment and financial meaning. |

**Priority:** Must have.

### US-004: Preserve research selection boundaries

**As a** research operator, **I want** V2 integration to retain training-only selection and exploratory provenance, **so that** diagnostic results cannot silently become promotion evidence.

```gherkin
Given a bounded research job with declared training and diagnostic windows
When the affected selection path executes and its result is revisited
Then eligible training evidence selects the configuration
And diagnostics remain separate with exploratory promotion restrictions intact
```

Acceptance: affected sweep/walk-forward/Discovery contracts retain the PR108 behavior, including latest-training-window evidence, provenance and invalid/legacy refusal. Exercise a bounded real research path where integration changes it; retain relevant owning regression checks. No independent final-validation/graduation certification is inferred.

| Condition | Expected outcome |
| --- | --- |
| Invalid/legacy exploratory promotion | Refused under the existing evidence policy. |
| Changed diagnostic values | Cannot change training selection. |

**Priority:** Must have.

### US-005: Preserve the accepted baseline during isolated development

**As an** operator, **I want** preserved data/results and coherent services, **so that** evaluating V2 does not compromise the accepted release or misrepresent live compatibility.

```gherkin
Given the accepted 1.223 release and historical experiments
When the isolated V2 research candidate starts and runs
Then historical results and baseline data remain available with their original meaning
And incompatible live behavior is explicit before any deployment is considered
```

Acceptance: complete candidate Linux application startup is demonstrated for relevant services; saved-result preservation and a compatible recovery path are documented/tested within this slice; no competing broker session or baseline state conversion occurs. Imports or native fixture success alone cannot certify service startup or live readiness.

| Condition | Expected outcome |
| --- | --- |
| Changed integration breaks an importing service | Candidate acceptance fails until repaired or an explicitly supported boundary is established. |
| Incompatible state format | Original copies remain preserved; recovery restores compatible code and state together. |

**Priority:** Must have.

## 5. Constraints and dependencies

- Use Nautilus native capabilities as the engine foundation, per the project rule; MSAI supplies the existing research/control/interface product.
- Preserve PR108 training selection and PR109 inclusive dates, with engine/data/assumption provenance for the candidate and historical experiments.
- Start from accepted release fb757953 plus local documentation reconciliation 9d51a63. That reconciliation is not yet published; no merge or deployment is implied by using it as the development base.
- The completed October 5 trial remains the baseline evidence. Exact release selection is a research/design decision and requires refreshed official sources.
- Existing authentication, operating guards and explicit account/environment identity remain applicable.
- Broader live migration and any production rollout retain separate lifecycle/state/risk, release and human-authorization gates.

## 6. Required security outcomes

- Preserve supported authentication and avoid secret/capability leakage in user errors, reports or retained evidence.
- Do not broaden account or trading permissions through research integration.
- Candidate work cannot mutate the running release, original data/cache or broker state without separate applicable authority.
- Record the engine, inputs, assumptions, environment and tested scope without claiming unexecuted acceptance or strategy alpha.

## 7. Open questions

Product scope follows the existing master plan. No additional product decision blocks this draft. Exact candidate pin, native integration contracts and affected files are resolved by research and plan review after approval; unresolved trial cases remain acceptance gates, not assumed passes.

## 8. References and approval

- [Discussion](nautilus-v2-research-discussion.md)
- [Master Plan](../../MASTER_PLAN.md#next-implementation-batch-one-real-research-journey-on-v2)
- [Completed native trial](../research/2026-10-05-nautilus-runtime-trial.md)
- [Accepted release summary](../../MASTER_MAP.md#released-assessment-integration-october-6)

| Version | Date | Change |
| --- | --- | --- |
| 1.0 | 2026-10-06 | Draft from the agreed narrow research integration scope. |

- [x] Product scope approved by the user — explicit “approved” reply on 2026-10-06, PRD v1.0.
- [x] Ready for research and technical design
