# PRD: ingest-backtest-smoke-test

**Version:** 1.3
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-26
**Last Updated:** 2026-05-26

---

## 1. Overview

MSAI v2 has an ingest pipeline (Databento → Parquet) and a portfolio backtest pipeline (FastAPI → arq → NautilusTrader → QuantStats → DB), but no single operator action drives the whole thing end-to-end on real market data and surfaces the resulting risk metrics in a usable shape. This PRD defines an **operational workflow smoke** — a thin, repeatable command that runs the canonical end-to-end flow on two real equity symbols (AAPL, SPY) using a multi-strategy portfolio (`smoke_market_order` for deterministic plumbing assertion + EMA Cross for business signal) across one of two named windows, and produces a normal Backtest record annotated with a structured risk-metrics block. (**v1.3 scope:** ES futures dropped from v1 per council verdict 2026-05-26; tracked as a near-term follow-up — see §7.)

Two canonical configurations:

- **`smoke:fast`** — 1-month window (the most recent calendar month covered by the canonical reproducibility pin), runtime budget tight enough to be merge-adjacent. Default for the CLI when no config is named, the UI button, and the opt-in deploy preflight.
- **`smoke:nightly`** — 2024-01-01 to 2024-12-31 (full calendar year), runtime budget larger, deeper confidence signal. Default for the scheduled nightly job.

Surfaces: CLI (`msai backtest smoke` defaults to `:fast`, `--config nightly` selects the deeper variant), UI button on the existing backtests page, nightly scheduled job, and an opt-in pre-deploy environment + data preflight. PASS/FAIL uses a two-tier check that distinguishes structural pipeline health (strict) from business-metric drift (annotation, never blocking). Risk metrics (total return, P&L, Sharpe, Sortino, alpha vs SPY benchmark, beta vs SPY, max drawdown, trade count by strategy) are first-class output on every run, not just when thresholds drift.

## 2. Goals & Success Metrics

### Goals

- **G1 — One operator action proves the pipeline AND shows the metrics.** An operator on the prod VM (or any authenticated user from the UI) can fire a single command and, within minutes, see whether the ingest+backtest pipeline still works end-to-end on real Databento data AND see the resulting risk metrics in a structured, machine-readable shape — not buried in a QuantStats HTML.
- **G2 — Catch pipeline regressions cheaply.** A nightly run surfaces broken ingest, broken Nautilus wiring, broken report generation, missing data subscriptions, or broken order submission BEFORE the operator notices on their next manual workflow.
- **G3 — Merge-adjacent pre-deploy sanity check is realistic.** The `smoke:fast` runtime budget is tight enough that an operator can opt in to a pre-deploy preflight on the path to merge without waiting an hour. The opt-in preflight is honestly framed as an _environment + data_ check, not a candidate-image validation.
- **G4 — Decouple pipeline health from business-metric noise.** Vendor data revisions, symbol mapping shifts, and benign strategy-drift do not break the gate; only structural pipeline failures (including the deterministic-trades floor described below) do.
- **G5 — Metrics are first-class output.** Every smoke run produces a structured risk-metrics block that the CLI prints as a human-readable table, exposes via `--json` for machine consumption, surfaces in the existing backtest details view in the UI, and includes in alert payloads. The QuantStats HTML report stays as the deep-dive artifact, not the primary output.

### Success Metrics

| Metric                                                                                | Target                                                          | How Measured                                                                                       |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `smoke:fast` time from "operator runs smoke" to "result visible" (Parquet present)    | ≤ 3 minutes p95                                                 | Wall-clock from CLI invocation / button click / scheduled trigger to terminal Backtest row state   |
| `smoke:fast` time from "operator runs smoke" to "result visible" (cold ingest)        | ≤ 10 minutes p95                                                | Same, when the canonical 1-month window Parquet files are absent and a Databento fetch is required |
| `smoke:nightly` time from "operator runs smoke" to "result visible" (Parquet present) | ≤ 10 minutes p95                                                | Same, against the 2024 full-year window with Parquet already on disk                               |
| `smoke:nightly` time from "operator runs smoke" to "result visible" (cold ingest)     | ≤ 60 minutes p95                                                | Same, when the 2024 full-year Parquet files are absent and a full Databento fetch is required      |
| Structural false-positive rate (structural FAIL but the pipeline is actually fine)    | 0 in any consecutive 14-day window                              | Manual triage of any structural FAIL in the alerts surface                                         |
| Business-metric warning rate (annotations that did not indicate a defect)             | Logged for observation; no target in v1                         | Count of `smoke-warning` alerts vs. operator triage outcome                                        |
| Nightly run reliability                                                               | ≥ 95% scheduled runs reach a terminal state within their budget | GitHub Actions run-history success rate                                                            |
| Operator adoption                                                                     | Pablo runs `msai backtest smoke` at least once post-deploy      | Self-reported during the first 4 weeks                                                             |

### Non-Goals (Explicitly Out of Scope)

- ❌ **No new `/api/v1/smoke-tests` resource.** Smoke results are stored as normal Backtest rows with a `smoke:fast` or `smoke:nightly` tag — the existing `/api/v1/backtests` surface is reused.
- ❌ **No new dedicated UI page.** The existing `/backtests` page gets a single "Run smoke" button; history reuses the same list view filtered on the `smoke` tag family.
- ❌ **No new top-level CLI sub-app.** Command lives at `msai backtest smoke`. No `msai smoke`, no `msai system smoke`.
- ❌ **No every-push pre-deploy gate.** Even at `smoke:fast` budget, the gate is opt-in; the default auto-deploy on push remains smoke-free.
- ❌ **No candidate-image validation in the opt-in gate.** US-004 smokes the currently-deployed image — it's honestly framed as an environment + data preflight, not a "your new code works" assertion. A candidate-image staging gate is a deferred follow-up.
- ❌ **No per-SHA pinned exact-metric reproducibility check.** Metrics are first-class OUTPUT every run; drift is annotated as warning context, never a pinned assertion that fails the run.
- ❌ **No multi-symbol fan-out beyond AAPL / SPY in v1.** Two equity symbols only. ES (CME futures) is dropped from v1 per council verdict 2026-05-26 — see §7 for the near-term follow-up.
- ❌ **No options data, no live trading, no order submission to IB.** Backtest only.
- ❌ **No operator-customized inputs on the UI button** (operators wanting custom backtests use the existing "New backtest" flow).
- ❌ **No rate limiting on the UI button in v1.** Authenticated-only + concurrent-ingest mutex (see §5) bound the Databento exposure; per-user rate limiting is risk-accepted.
- ❌ **No multi-tenant / multi-account scoping** — the smoke runs against the single canonical configuration only.
- ❌ **No retroactive backfill or historical smoke history migration** — the feature ships forward-only.
- ❌ **No tier upgrade on the Databento subscription.** The smoke uses OHLCV 1-minute bars (core schema), which under the current Databento Standard plan has entire-history coverage — both canonical windows fit without changing the subscription.

## 3. User Personas

### Operator (Pablo) — primary

- **Role:** Founding operator / developer running MSAI v2 on dev laptops and the prod Azure VM.
- **Permissions:** Full access — SSH to VM, Entra ID admin, repo write, deploy-workflow dispatcher.
- **Goals:**
  - Confirm a freshly-deployed prod build can actually backtest with real Databento data — and see the risk metrics — before trusting it.
  - Catch a broken ingest or broken Nautilus wiring overnight, not days later.
  - Selectively run a pre-deploy environment preflight for risky deploys without slowing the normal deploy cadence.
  - Read the risk metrics directly from stdout or alert payload, not by opening a QuantStats HTML.

### Authenticated dashboard user

- **Role:** Any Entra-authenticated user reaching the MSAI v2 web UI (today: Pablo; future: small team).
- **Permissions:** Standard authenticated access (same as the rest of `/backtests` today).
- **Goals:**
  - Fire the canonical `smoke:fast` from a single button on the existing backtests page.
  - Browse smoke runs separately from ad-hoc backtests in the history view.
  - See the structured risk metrics in the existing backtest details view without downloading the QuantStats HTML.

### Automated nightly job

- **Role:** GitHub Actions scheduled workflow running unattended at 05:00 UTC.
- **Permissions:** Read-only repo + workflow privileges; SSH into the VM under the existing deploy-credential model.
- **Goals:**
  - Run `smoke:nightly` against the live VM stack every night.
  - Post the metrics block + any business-metric drift annotations to `/api/v1/alerts` so an operator can triage on next login.
  - Fail the workflow run on structural failure (including deterministic-trades floor) so the alert is unmissable.

### Opt-in pre-deploy preflight (the same workflow invoked manually)

- **Role:** A preflight stage in `deploy.yml`, gated by a `workflow_dispatch` input, invoked on demand by the operator before a deploy they want extra sanity on.
- **Permissions:** Same as the nightly job.
- **Goals:**
  - Prove the prod VM environment + Databento path + Nautilus pipeline are healthy _as they currently exist_ before deploying anything new.
  - Block the downstream `deploy.yml` deploy stage when structural smoke fails — preventing a deploy onto an already-broken VM environment.
  - **NOT** claim to validate the candidate image. That is explicitly deferred to a follow-up (see Open Questions).

## 4. User Stories

### US-001: Operator runs the smoke from the CLI and reads risk metrics from stdout

**As an** operator on the prod VM
**I want** to run `msai backtest smoke` and immediately see a structured risk-metrics block plus a clear PASS/FAIL line
**So that** I can confirm the pipeline works AND read the metrics without opening an HTML report.

**Scenario:**

```gherkin
Given Pablo is SSHed into the prod VM with a working .env (Databento key, DB, Redis available)
And the canonical smoke:fast Parquet files (AAPL, SPY — 1-month window) are already present from a prior run
When Pablo runs `msai backtest smoke`
Then the command resolves the canonical smoke:fast configuration (1-month window, AAPL + SPY, smoke_market_order + EMA Cross multi-strategy portfolio)
And the command checks each canonical Parquet window and skips ingest where files exist
And the command submits the canonical multi-strategy portfolio backtest
And the command polls until the backtest reaches a terminal state
And on STRUCTURAL pass the stdout prints a structured risk-metrics table (return, P&L, Sharpe, Sortino, alpha vs SPY, beta vs SPY, max drawdown, trade count per strategy, benchmark, backtest id, report path) followed by "PASS — Backtest <id>" and exits 0
And on STRUCTURAL failure the command prints the failing stage + reason + remediation hint and exits non-zero
And on BUSINESS metric drift the metrics block ANNOTATES the drifted metric (e.g., "Sharpe: 0.62 ↓ below baseline 0.85") AND posts an alert to /api/v1/alerts tagged smoke-warning, but still exits 0
And `msai backtest smoke --json` outputs the same metrics block as a machine-readable JSON document on stdout (no human-readable framing)
And `msai backtest smoke --config nightly` selects the smoke:nightly configuration (2024 full-year window)
```

**Acceptance Criteria:**

- [ ] `msai backtest smoke` (no args) runs `smoke:fast` by default.
- [ ] `msai backtest smoke --config nightly` runs `smoke:nightly`.
- [ ] `msai backtest smoke --json` outputs the structured metrics block as a single JSON document on stdout — suitable for piping to `jq` or capturing in CI logs.
- [ ] Stdout includes a structured metrics block with: total return, P&L, Sharpe, Sortino, alpha vs SPY, beta vs SPY, max drawdown, trade count broken down per strategy, benchmark identifier (`SPY`), backtest id, report path. The block format is stable enough to grep / parse.
- [ ] Stdout includes the per-stage status lines (ingest-check, ingest, backtest-submit, backtest-poll, report-check) for forensic triage.
- [ ] Exit code: 0 on structural pass; non-zero on structural fail. Business drift annotations do NOT change the exit code.
- [ ] On structural failure: command stops at the first broken stage, persists any partial artifacts (Nautilus logs, partial report) to disk under a deterministic path, prints the path, and exits non-zero with a remediation hint.
- [ ] `smoke:fast` runtime budget: ≤ 3 min p95 warm, ≤ 10 min p95 cold.
- [ ] `smoke:nightly` runtime budget: ≤ 10 min p95 warm, ≤ 60 min p95 cold.
- [ ] No new database table is created; the run lands as a normal Backtest row tagged `smoke:fast` or `smoke:nightly` (singular tag carrying the config name) in the existing `/api/v1/backtests` surface.
- [ ] Concurrent smoke invocations DO NOT trigger parallel Databento ingest for overlapping windows — the second invocation's ingest waits behind the first (mutex on the ingest layer). Backtest execution itself runs in parallel once data is on disk.

**Edge Cases:**

| Condition                                                                            | Expected Behavior                                                                                                                                                                                                                                                                                             |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Databento auth missing / invalid (cold ingest path)                                  | Structural FAIL at ingest stage; stderr names the env var; non-zero exit; no partial Backtest row written.                                                                                                                                                                                                    |
| Databento returns partial data for the requested window                              | Structural FAIL at ingest stage with the specific window gap reported; no backtest is submitted.                                                                                                                                                                                                              |
| Backtest worker container is not running                                             | Structural FAIL at backtest-submit with "worker unreachable"; non-zero exit.                                                                                                                                                                                                                                  |
| Backtest job times out                                                               | Structural FAIL after the configured timeout; partial Nautilus logs persisted; the Backtest row is marked failed; non-zero exit.                                                                                                                                                                              |
| Backtest produces FEWER THAN 2 trades (the deterministic-trades floor)               | Structural FAIL. The portfolio includes one `smoke_market_order` strategy row per equity symbol (AAPL, SPY); that strategy submits exactly ONE market order on first bar per instrument, so the v1 floor is 2 trades. Fewer than 2 means order submission, fill plumbing, or instrument resolution is broken. |
| `smoke_market_order` produces 3 trades but EMA Cross produces 0 trades               | Structural PASS (plumbing healthy). Business WARN posted ("EMA Cross fired 0 trades — strategy may need parameter review"). Exit 0.                                                                                                                                                                           |
| AAPL or SPY is missing from the operator's Databento entitlement                     | Structural FAIL at ingest stage; the error message names the missing symbol and points to the Databento-entitlement remediation hint; the partner symbol is NOT ingested in the same run.                                                                                                                     |
| Concurrent smoke invocations (CLI + UI fire near-simultaneously) for the same config | Both runs accepted and produce distinct Backtest rows; the second run's ingest stage waits behind the first run's mutex; backtests run in parallel once data is on disk.                                                                                                                                      |
| Operator passes unknown flags                                                        | The CLI fails fast with the usage banner; no Backtest row is created.                                                                                                                                                                                                                                         |

**Priority:** Must Have

---

### US-002: Authenticated user runs the smoke from the UI and reads metrics in the backtest details view

**As an** authenticated user on the MSAI v2 dashboard
**I want** a "Run smoke" button on the existing `/backtests` page that fires the canonical `smoke:fast` configuration and surfaces the metrics block in the existing backtest details view
**So that** I can verify the pipeline and see the risk metrics without leaving the dashboard.

**Scenario:**

```gherkin
Given I am authenticated on the dashboard
And I am on the /backtests page
When I click "Run smoke"
Then the UI confirms the submission immediately (the page does not stall)
And a new Backtest row appears in the existing history list, tagged smoke:fast, in a "pending" / "running" state
And the row's status updates live to "passed" or "failed" without manual refresh
And on passed I can click the row to open the existing backtest details view
And the existing backtest details view shows the structured risk-metrics block (return, P&L, Sharpe, Sortino, alpha vs SPY, beta vs SPY, max drawdown, trade count per strategy) prominently, with the QuantStats HTML link offered as a secondary "Open full report" affordance
And on failed the row shows the failing stage and a link to the persisted Nautilus logs
```

**Acceptance Criteria:**

- [ ] The "Run smoke" button is present on the existing `/backtests` page and is visible to any authenticated user.
- [ ] The button submits the canonical `smoke:fast` configuration; no operator-tweakable inputs are exposed in v1.
- [ ] After click, the user can see the new run in the existing backtests history list within 2 seconds, tagged `smoke:fast`, in a non-terminal state.
- [ ] The row's status transitions live to a terminal state.
- [ ] The existing backtest details view surfaces the structured risk-metrics block as primary content (not buried behind a "Download report" link).
- [ ] On failure, the existing backtest details view surfaces the failing stage and a link to the persisted Nautilus logs.
- [ ] The smoke runs can be filtered/browsed in the backtests history via the `smoke` tag family using the existing history view's filter mechanism (or a new chip if no equivalent exists today).
- [ ] No new page, route, or dedicated UI is added — the entire smoke experience lives within the existing backtests page family.
- [ ] The structured metrics block is rendered from the same data shape the CLI emits via `--json` — no parallel format.

**Edge Cases:**

| Condition                                             | Expected Behavior                                                                                                                                          |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| User is not authenticated                             | Existing auth redirect; button is not reachable.                                                                                                           |
| User clicks "Run smoke" twice rapidly                 | Each click submits a separate run (no idempotency in v1); both rows appear in history with distinct IDs; ingest mutex prevents parallel Databento fetches. |
| The backend is unreachable when the button is clicked | UI shows a non-blocking error toast naming the failure; no Backtest row is created.                                                                        |
| The user navigates away mid-run                       | The run continues server-side; user sees the terminal state next time they visit `/backtests`.                                                             |

**Priority:** Must Have

---

### US-003: Nightly automated smoke surfaces regressions overnight with the metrics block in the alert payload

**As an** automated nightly job
**I want** to run `smoke:nightly` against the live VM stack at 05:00 UTC and post the metrics block + any business-metric drift annotations to the alerts surface
**So that** structural pipeline regressions wake someone up, and the next morning's triage starts from a structured metrics view rather than an HTML.

**Scenario:**

```gherkin
Given the GitHub Actions scheduled workflow fires at 05:00 UTC
And the prod VM is reachable via the existing SSH-credential model used by deploy.yml
When the workflow runs the canonical smoke:nightly against the live stack
Then the workflow exits non-zero if and only if the smoke STRUCTURAL tier fails (including the deterministic-trades floor)
And the resulting Backtest row is tagged "smoke:nightly" and visible in the standard /api/v1/backtests history
And ONE alert is posted to /api/v1/alerts with kind=smoke-result carrying the structured metrics block — regardless of PASS/FAIL — so the operator can see metrics overnight without logging into the dashboard
And IF business-metric drift is detected, the drifted metric is annotated on the metrics block (e.g., "Sharpe ↓ below baseline") in the same alert payload
And the workflow run's GitHub Actions log captures the per-stage status lines from the CLI
```

**Acceptance Criteria:**

- [ ] A new scheduled GitHub Actions workflow exists that fires at 05:00 UTC. The cron string is a literal at the top of the workflow file (GitHub Actions cannot resolve `vars.*` inside `schedule:` at parse time — confirmed in research 2026-05-26). Retuning the cron is a small PR; this is acceptable for a single-operator project.
- [ ] The workflow runs `smoke:nightly` ON the prod VM via the same SSH path `deploy.yml` already uses (no new credential model).
- [ ] The workflow's job exit code matches the smoke's STRUCTURAL tier — non-zero on structural fail, zero otherwise.
- [ ] Each nightly run produces exactly ONE alert in `/api/v1/alerts` tagged `smoke-result` with the structured metrics block, regardless of PASS/FAIL.
- [ ] Any business-metric drift detected is captured as annotations on the same alert's metrics block (no separate alert per drifted metric).
- [ ] The resulting Backtest row is queryable via the existing `/api/v1/backtests` history with a `smoke:nightly` tag filter and is the same shape as a UI- or CLI-initiated smoke row.
- [ ] The workflow log includes the smoke CLI's stderr per-stage status lines for forensic triage.
- [ ] If the nightly workflow itself cannot reach the VM (SSH failure, NSG misconfiguration), the workflow fails with a clear cause line and does NOT post a smoke-result alert (this is a workflow-infrastructure failure, not a smoke signal).

**Edge Cases:**

| Condition                                                    | Expected Behavior                                                                                                                                                                   |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prior nightly run's Parquet files are still present          | Skip ingest; finish well inside the 10-min p95 warm budget.                                                                                                                         |
| Databento publishes a corrected bar set between nightly runs | If files exist, no re-ingest happens; drift surfaces as a metric annotation in the alert.                                                                                           |
| VM is in the middle of a deploy when the nightly fires       | The smoke either passes (post-deploy probes already cleared) or structurally fails; if it FAILs, the alert names "deploy in progress" as a likely cause but does not auto-rollback. |
| Alert posting fails (alerts endpoint down)                   | The workflow exits non-zero with an "alert dispatch failed" cause line, so the operator still sees the failure even if alerts are broken.                                           |

**Priority:** Must Have

---

### US-004: Operator opt-in pre-deploy ENVIRONMENT + DATA preflight

**As an** operator about to deploy a higher-risk change
**I want** to invoke `smoke:fast` as a pre-deploy environment preflight via a `workflow_dispatch` input on `deploy.yml`
**So that** I can confirm the prod VM environment, Databento path, and Nautilus pipeline are healthy BEFORE the deploy stage swaps in the new image — without claiming to validate the candidate code itself.

> **Honest framing:** This preflight runs against the CURRENTLY-DEPLOYED image, not the candidate. It catches a broken VM environment or broken Databento path that the deploy would otherwise inherit. It does NOT validate that the new code works. Candidate-image validation requires a staging/ephemeral path; that is a deferred follow-up (see §7).

**Scenario:**

```gherkin
Given Pablo is about to deploy SHA <sha> and wants an environment preflight first
When Pablo invokes `gh workflow run deploy.yml -f git_sha=<sha> -f run_smoke=true`
Then deploy.yml's preflight stage runs smoke:fast ON the VM via SSH against the currently-deployed image
And on STRUCTURAL pass deploy.yml proceeds to the existing deploy stage
And on STRUCTURAL fail deploy.yml stops before the deploy stage and surfaces the failing-stage cause
And on BUSINESS-metric drift deploy.yml proceeds AND posts the annotated metrics block to /api/v1/alerts
And the workflow log clearly states "preflight = environment + data preflight, NOT candidate-image validation" so future operators don't misread its scope
And the default auto-deploy on push to main remains preflight-free (no behavior change for normal pushes)
```

**Acceptance Criteria:**

- [ ] `deploy.yml` accepts a `workflow_dispatch` input named `run_smoke` (boolean, default false).
- [ ] When `run_smoke=true`, a preflight stage runs `smoke:fast` before the existing deploy stage. It uses the same SSH/VM model as the nightly workflow.
- [ ] When `run_smoke=true` and the smoke STRUCTURAL tier fails, `deploy.yml` exits non-zero before invoking the deploy stage. No image is deployed.
- [ ] When `run_smoke=true` and the smoke STRUCTURAL tier passes, `deploy.yml` proceeds to the existing deploy stage unchanged.
- [ ] When `run_smoke=true` and the smoke posts business-metric annotations, `deploy.yml` STILL proceeds to deploy (annotations do not block) but the metrics block appears in `/api/v1/alerts` tagged `smoke-result`.
- [ ] When `run_smoke=false` or absent (i.e., normal push-triggered deploy), `deploy.yml` behavior is identical to today — no preflight stage, no extra runtime.
- [ ] The workflow log clearly shows the smoke stage's per-stage status lines AND explicitly states the preflight scope ("environment + data, NOT candidate code").
- [ ] The opt-in is documented in `docs/how_to_deploy.md` so future operators discover it via the existing deploy runbook AND understand what it does and does not validate.

**Edge Cases:**

| Condition                                                                               | Expected Behavior                                                                                                                                                                               |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The preflight stage takes longer than the workflow's typical SLA window                 | The workflow times out via the existing job-timeout setting; failure is surfaced; no image is deployed.                                                                                         |
| `git_sha` is older than the currently-deployed VM image                                 | The preflight runs against the currently-deployed image (not the requested SHA); the cause line in the workflow log makes this explicit.                                                        |
| The smoke STRUCTURAL passes but the subsequent deploy stage fails for unrelated reasons | Existing deploy-stage error handling applies; the preflight result is unaffected.                                                                                                               |
| The operator forgets `run_smoke=true` on a deploy they meant to gate                    | No safety net — the deploy proceeds without preflight. The opt-in is intentional; future hardening (PR-label-driven, commit-trailer-driven, candidate-image staging) is deferred to follow-ups. |

**Priority:** Should Have

---

## 5. Constraints & Policies

### Business / Compliance Constraints

- **Real money path stays untouched.** The smoke does NOT submit orders to IB Gateway, does NOT touch any live deployment, and does NOT depend on any IB account state. It is a backtest workflow only.
- **Databento subscription:** The smoke uses OHLCV 1-minute bars (a core schema), which under the operator's current Databento Standard plan ($199/month, 2026-05) has **entire-history coverage** — both canonical windows fit without changing the subscription tier. If at any future point the smoke is reshaped to use Level 1 (TBBO/trades, 12-month rolling) or Level 2/3 (MBP-10/MBO, 1-month rolling) schemas, the configuration MUST move to a rolling window (e.g., "most recent complete month") or the subscription tier MUST be upgraded — out of scope for v1.
- **No persistent storage of customer / third-party PII.** The smoke operates only on market data and produces only Backtest rows + QuantStats reports — both already covered by existing data-handling policy.
- **Databento spend ceiling:** Concurrent smoke invocations MUST NOT cause parallel Databento fetches for overlapping windows. A mutex on the ingest layer ensures the second invocation waits for the first to finish (re-checking Parquet presence). This is the v1 cost-control mechanism in lieu of per-user rate limiting.

### Platform / Operational Constraints

- **VM resource budget.** The smoke runs on the existing prod VM (Standard_D4ds_v6 today) and must coexist with normal backend/worker traffic. Memory, CPU, and Parquet disk footprint must stay within the current dev/prod profile — no resource-class bump permitted as part of this feature.
- **Runtime budgets:**
  - `smoke:fast` — ≤ 3 min p95 warm, ≤ 10 min p95 cold. Anything beyond 10 min cold is a structural failure (workflow timeout).
  - `smoke:nightly` — ≤ 10 min p95 warm, ≤ 60 min p95 cold. Anything beyond 60 min cold is a structural failure.
- **Schedule.** Nightly cron at 05:00 UTC. The cron string is a literal in the workflow YAML (GitHub Actions does not support `vars.*` interpolation inside `schedule:` at workflow-parse time, confirmed via research). Retuning the cron requires a small PR; acceptable trade-off for a single-operator project.
- **CI minute budget.** The opt-in deploy-preflight path is the only CI surface that consumes runner minutes for the smoke proper. The nightly path uses the VM (not the runner) for the smoke work itself; the runner only orchestrates SSH.
- **No new Docker image, no new container.** The smoke runs inside the existing backend / worker containers via the existing arq job pipeline. No new docker-compose service.
- **Strategy guarantees a non-empty trade signal:** The canonical portfolio MUST include the deterministic `smoke_market_order` strategy on every instrument. That strategy submits exactly one market order on the first bar per instrument, so a 3-instrument portfolio guarantees ≥ 3 trades on every smoke run. The deterministic-trades floor of 3 is a STRUCTURAL invariant. Strategies added to the portfolio later (EMA Cross, others) provide business signal but are not counted toward the floor.

### Dependencies & Required Integrations

- **Requires (existing):**
  - Databento Standard plan with equities + futures core-schema (OHLCV) coverage — current subscription is sufficient (no tier upgrade required for v1).
  - The existing ingest pipeline (`msai ingest`) and Parquet store under `DATA_ROOT`.
  - The existing backtest pipeline (`POST /api/v1/backtests/run` or the equivalent portfolio-backtest path — to be confirmed in research).
  - The existing `/api/v1/backtests` history and details views, capable of rendering the structured metrics block as primary content.
  - The existing `/api/v1/alerts` surface (used for the metrics + drift channel).
  - The existing Azure Entra ID auth + `MSAI_API_KEY` dev/CLI path.
  - The existing `deploy.yml` workflow + SSH-into-VM credential model.
  - The existing `strategies/example/smoke_market_order.py` (deterministic 1-order-per-bar strategy) and `strategies/example/ema_cross.py`.
- **Named integrations (scope, not mechanism):**
  - **Databento** — for historical equity + futures bar data (OHLCV 1-minute bars).
  - **GitHub Actions** — for the nightly schedule and the opt-in deploy-preflight `workflow_dispatch`.
- **Blocked by:** None known. The existing portfolio backtest path is presumed to support the multi-strategy submission shape required by the canonical config; if not, scope changes to two single-strategy backtests per symbol (3 × 2 = 6 backtests under a synthetic smoke-run parent) — to be resolved in `/new-feature` Phase 2 research.

## 6. Security Outcomes Required

- **Who can access what:**
  - The CLI `msai backtest smoke` requires the operator to already have the existing `MSAI_API_KEY` or a valid Entra-issued backend audience JWT.
  - The UI button is reachable only to authenticated users (existing dashboard auth boundary).
  - The nightly + opt-in workflows reuse the existing `deploy.yml` credential model — no new long-lived secret.
- **What must never leak:**
  - The Databento API key must never be logged, included in stdout, included in alert payloads, or printed in the QuantStats report.
  - The Backtest row + report blob must never reveal raw Databento entitlement details (only `missing entitlement for <symbol>` as a remediation hint — never the raw vendor response).
- **What must be auditable:**
  - Every smoke run (CLI, UI, nightly, opt-in preflight) produces a normal Backtest row in `/api/v1/backtests` with timestamp, actor (where derivable), tag (`smoke:fast` or `smoke:nightly`), strategy code hashes, and a stable report path. The existing backtest data-lineage fields (`nautilus_version`, `python_version`, `data_snapshot`) are sufficient.
  - Every nightly run produces exactly one `/api/v1/alerts` entry with kind `smoke-result` carrying the structured metrics block + any drift annotations (Backtest row id, config name, all metric values, benchmark used, threshold context where annotated).
- **What legal/regulatory outcomes apply:** None new. The smoke does not change the existing data-handling, real-money, or accountability surfaces.

## 7. Open Questions

- [ ] **Multi-strategy portfolio backtest submission shape.** Does the existing `POST /api/v1/backtests/run` accept a multi-strategy payload that yields a SINGLE Backtest row, or does the smoke need to go through `/api/v1/live-portfolios/` (revision-frozen) with a backtest-mode flag, or does v1 fall back to 6 single-strategy backtests (3 instruments × 2 strategies) under a synthetic smoke-run parent? **Resolved in `/new-feature` Phase 2 research-first agent.**
- [ ] **Exact business-tier drift thresholds.** Specific numeric thresholds for "Sharpe drift", "alpha/beta drift", "trade-count drift" are placeholders in the design and will be calibrated from an initial baseline run in Phase 5 implementation, then committed alongside the smoke config. Thresholds are ANNOTATION triggers, not pass/fail thresholds (per G4).
- [ ] **`msai backtest smoke` operator-override flags.** Whether to include `--re-ingest` (force re-download) and `--force` (no-op if already-passed within N hours) is convenience; defer the decision to design phase. Default behavior (skip ingest if Parquet present; always re-run backtest) is fixed.
- [ ] **Alerts surface enrichment.** Whether `/api/v1/alerts` currently has a `kind=smoke-result` taxonomy slot and can carry a structured metrics block in its payload, or whether the alert schema needs extension; surface in design.
- [ ] **Backtest history filter mechanism.** Whether the existing `/backtests` history view today supports filtering by tag (or tag family — i.e., "all smoke runs regardless of `:fast` / `:nightly` suffix"), or whether US-002's filter requirement implies a new filter chip / query parameter; surface in design.
- [ ] **Backtest details view metrics-block prominence.** Whether the existing backtest details view already renders a structured metrics block prominently, or whether US-002's "primary content" requirement implies UI work to promote the metrics block above the report-download affordance; surface in design.
- [ ] **`smoke:fast` window canonicalization.** Whether the 1-month window is pinned to a specific calendar month (e.g., "2024-12-01 to 2024-12-31") for reproducibility, OR floats with the calendar ("most recent completed calendar month"). The pinned choice is more reproducible for the deploy-preflight use case; the floating choice catches recent-data ingest regressions in the nightly use case. May resolve as: `smoke:fast` is pinned for reproducibility, `smoke:nightly` is the rolling check. Surface in design.
- [ ] **Candidate-image staging gate (deferred follow-up).** US-004 honestly admits it does not validate the candidate image. A future follow-up could spin up the candidate image in an ephemeral staging compose, run `smoke:fast` against it, and only then promote to deploy. Tracked as out-of-scope for v1; surface as a follow-up issue once v1 ships.
- [ ] **Benchmark choice (alpha/beta).** v1 uses SPY as the benchmark for alpha/beta calculations across the two-equity portfolio (one of the canonical symbols, already ingested). Reuses the existing `compute_alpha_beta` helper at `backend/src/msai/services/analytics_math.py:164`.
- [ ] **ES (CME futures) follow-up — near-term/blocking for futures coverage.** Council 2026-05-26 dropped ES from v1 because `resolve_instrument()` is equity-only (`backend/src/msai/services/nautilus/instruments.py:23-41`) and allocations have a single `asset_class` defaulting to `stocks`. The follow-up PR must: (i) extend `resolve_instrument()` to handle continuous-futures symbology (`ES.c.0`), (ii) extend the allocation `asset_class` enum to cover futures, (iii) add GLBX.MDP3 / `stype_in=continuous` routing to the smoke pre-ingest path, (iv) bump the deterministic-trades floor from 2 → 3 (one `smoke_market_order` per added symbol), (v) update CLAUDE.md "Goal" line to reflect 3-symbol coverage. Tracking: open immediately upon v1 merge; treat as next-feature priority, not backlog.

## 8. References

- **Discussion Log:** `docs/prds/ingest-backtest-smoke-test-discussion.md`
- **Related PRDs:**
  - `docs/prds/backtest-auto-ingest-on-missing-data.md` — adjacent feature for on-demand ingest when a UI-initiated backtest hits a data gap
  - `docs/prds/backtest-failure-surfacing.md` — establishes the failure-surfacing pattern this PRD reuses
  - `docs/prds/backtest-results-charts-and-trades.md` — the existing /backtests details view this PRD extends with the structured metrics block
  - `docs/prds/databento-registry-bootstrap.md` — the instrument-registry path the futures-roll handling depends on
- **Codex consultation:** Three `codex exec` rounds during PRD discussion + review (recorded in the discussion file): framing decision ("B-minus" / operational workflow smoke), tie-break on PASS-check shape + CLI placement, and the v1.1 revision review.
- **Databento subscription research (2026-05-26):** Standard plan ($199/month) provides core schemas (OHLCV) with **entire-history coverage**; L1 (top-of-book) with 12-month rolling; L2/L3 (MBP-10/MBO) with 1-month rolling. The smoke uses OHLCV; both canonical windows fit without a tier upgrade. Source: `databento.com/pricing` and Codex-mediated WebSearch (2026-05-26).
- **Project goal reference:** `CLAUDE.md ## Project Overview ## Goal` — "First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data."

---

## Appendix A: Revision History

| Version | Date       | Author                                       | Changes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------- | ---------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-05-26 | Claude + Pablo                               | Initial PRD.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 1.1     | 2026-05-26 | Claude + Pablo + Codex                       | Split canonical config into `smoke:fast` (1-month, merge-adjacent budget) + `smoke:nightly` (2024 full-year, deeper confidence). Promoted structured risk-metrics block to first-class output on every run (G5). Reframed US-004 as honest "environment + data preflight" instead of pretending to validate the candidate image. Hardened "zero trades" by leaning on `smoke_market_order`'s deterministic-trades floor of 3. Resolved Databento subscription research (Standard plan OHLCV = entire history). Added ingest-mutex constraint. Defaulted CLI to `smoke:fast`; `--config nightly` opts into the deeper run. Defaulted alpha/beta benchmark to SPY. |
| 1.2     | 2026-05-26 | Claude + research-first                      | Downgraded US-003 AC #1 + §5 schedule constraint: cron is a YAML literal, NOT configurable via repo Variable (GitHub Actions parse-time limitation, confirmed via `databento.com/pricing`-research-pass open-risk #1). Retuning requires a small PR; acceptable for a single-operator project. No other AC or scope change.                                                                                                                                                                                                                                                                                                                                      |
| 1.3     | 2026-05-26 | Claude + Council 5-advisor (chairman: Codex) | Dropped ES (CME futures) from v1 per council verdict (APPROVE_A 4-1; Maintainer dissented APPROVE_B). v1 ships AAPL+SPY only; deterministic-trades floor adjusted ≥3 → ≥2; ES tracked as near-term follow-up under §7 (extend `resolve_instrument`, `asset_class` enum, GLBX.MDP3 routing, bump floor back to ≥3). Council rationale: ES requires extending shared instrument-resolution + asset-class plumbing called from 6+ sites; that's a futures-routing feature, not a smoke test. CLAUDE.md project goal literally says AAPL/SPY anyway.                                                                                                                 |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design
