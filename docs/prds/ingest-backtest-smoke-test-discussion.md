# PRD Discussion: ingest-backtest-smoke-test

**Status:** Complete
**Started:** 2026-05-26
**Participants:** Pablo, Claude

## Original User Stories

Inferred from `CLAUDE.md` project goal: _"First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data."_

Plus prior session memory:

- The pipeline (Databento ingest → Parquet catalog → BacktestNode → QuantStats) has been built but has never been driven end-to-end on a real symbol from a clean state by a single operator action.
- Three known bug classes have bitten this path: (1) Databento EQUS.MINI 2015 history requires subscription upgrade, (2) Nautilus-typed `default_config` fields drop through registry walker, (3) `GraduationCandidate.config` snapshots `strategy.default_config` and goes stale.

Implicit goal: produce a repeatable, single-command "smoke test" that an operator (Pablo) can run to prove the ingest + backtest pipeline works on real market data, and that surfaces a clear PASS / FAIL signal plus the resulting metrics.

## Discussion Log

### Round 1 — Foundational scope

**Q: Actor running the smoke test?**
A: All three — CLI + UI + scheduled.

**Q: PASS/FAIL surface?**
A: PASS = non-zero trades + QuantStats report path (later refined to a two-tier shape — see Round 4 / Codex Q1).

**Q: Scope of data + strategy on first pass?**
A: Multi-symbol + multi-strategy (AAPL + SPY + ES, EMA Cross + smoke_market_order, portfolio backtest).

**Q: Idempotency / re-run behavior?**
A: Skip ingest if Parquet already present; always re-run backtest.

### Round 2 — Symbol set, strategy set, window, failure UX

**Q: Which symbol set?**
A: AAPL + SPY + ES (CME futures — Databento sub already confirmed covers ES 2024 minute bars).

**Q: Which strategies?**
A: EMA Cross + smoke_market_order, multi-strategy portfolio backtest (one TradingNode running both).

**Q: Time window?**
A: Fixed pinned window — 2024-01-01 to 2024-12-31.

**Q: Failure UX?**
A: Stop at first failure, but on backtest failure persist artifacts (Nautilus logs, partial report) before exiting.

### Round 3 — Pablo pushback: "this tastes like an E2E test"

Pablo questioned whether the feature is materially different from new E2E coverage. Claude proposed three framings (A=E2E + CI wiring, B=thin wrapper around existing backtest pipeline, C=full first-class smoke-tests product resource) and routed the decision to Codex.

### Round 4 — Codex framing + locked decisions

**Codex verdict (verbatim summary):**

- Framing: **B-minus** — operational workflow smoke (NOT test-suite E2E). Pablo's "tastes like E2E" instinct is partially right (it crosses ingest → storage → worker → Nautilus → report → DB/API/UI) but it's stateful, externally dependent, business-meaningful — that's a "production workflow smoke," not an E2E test.
- Keep Backtest row tagged `smoke` in `/api/v1/backtests` — gives operator audit trail, UI visibility, report path, config, timestamps.
- **Reject every-push pre-deploy gate.** Realistic runtime budget is 5-20 min (Parquet present) to 15-60+ min (re-ingest from Databento); on a Standard_D4ds_v6 plus vendor-network variability, blocking every push is too expensive and too flaky.
- "Non-zero trades" is a **business assertion** — fails for non-build reasons (vendor data revisions, symbol mapping, strategy drift). Don't make it a deploy-blocking gate.

**Q1 — PASS-check shape (Codex chose):**
A: **(a) Two-tier.**

- STRUCTURAL strict: pipeline runs to completion, Backtest row written, report file produced, no Nautilus exceptions, all stages reached → CI exit non-zero on failure.
- BUSINESS warn-only: trade count > 0, Sharpe > X threshold, P&L within ±eps → posts to alerts surface, does NOT fail run.

**Q2 — CLI placement (Codex chose):**
A: **(a) `msai backtest smoke`** under the existing backtest sub-app.

### Round 5 — Operational details

**Q: CI placement?**
A: Codex-recommended: nightly scheduled smoke + manual pre-release + optional deploy-gate switch (NOT every-push gate).

**Q: Where do BUSINESS warnings surface?**
A: Post to `/api/v1/alerts` tagged `smoke-warning`.

**Q: Scheduled nightly cron time?**
A: ~05:00 UTC (after US market close + Databento daily finalization) — avoids transient vendor-revision false alarms.

**Q: How does operator activate the optional deploy gate?**
A: `workflow_dispatch` input on `deploy.yml`: `run_smoke=true`. Auto-deploy on push stays smoke-free. Operator triggers smoke-gated deploys via `gh workflow run deploy.yml -f git_sha=<sha> -f run_smoke=true`.

**Q: UI button config?**
A: Same fixed canonical config — no operator-tweakable inputs on the smoke button. (Operators who want custom backtests use the existing "New backtest" flow.)

**Q: UI auth?**
A: Any authenticated user.

---

## Refined Understanding

### Personas

- **Operator (Pablo / future ops user) from the CLI** — runs `msai backtest smoke` on dev or prod VM to prove the pipeline end-to-end. Primary persona.
- **Any authenticated user from the UI** — clicks "Run smoke" button on `/backtests` page; result lands in the existing backtests history.
- **Scheduled nightly job + optional opt-in deploy gate** — automated runs by GitHub Actions; no human in the loop except when alerts fire.

### User Stories (Refined)

- **US-001:** As an operator on the prod VM, I want to run `msai backtest smoke` and see a clear PASS/FAIL line plus a QuantStats report path, so I can confirm a freshly-deployed build can actually backtest with real Databento data.
- **US-002:** As any authenticated user on the dashboard, I want a "Run smoke" button on the `/backtests` page that fires the canonical smoke configuration, so I can verify the pipeline from the UI without remembering symbols/window/strategies.
- **US-003:** As an automated nightly job, I run the smoke at 05:00 UTC and post any business-metric drift to `/api/v1/alerts` tagged `smoke-warning`, so structural pipeline regressions wake someone up but data-revision flake does not.
- **US-004:** As an operator about to do a risky deploy, I want to trigger `gh workflow run deploy.yml -f git_sha=<sha> -f run_smoke=true` and have the workflow run the smoke ON the VM via SSH and only proceed with the deploy on STRUCTURAL pass, so high-risk deploys get a real-data sanity check without making every-push deploys slow.

### Non-Goals (Explicit Exclusions)

- No new `/api/v1/smoke-tests` resource — smoke results are normal Backtest rows with a `smoke` tag.
- No new dedicated UI page — reuses `/backtests` plus a button.
- No new top-level CLI sub-app (no `msai smoke`, no `msai system smoke`).
- No automatic every-push pre-deploy gate — opt-in only.
- No deterministic exact-metric reproducibility check — business tier is warn-only.
- No multi-symbol fan-out beyond AAPL / SPY / ES in v1.
- No options data, no live trading, no order submission.
- No rate limiting on the UI button (v1) — risk accepted; only authenticated users.
- No support for operator-customized smoke configs in the UI button (operators wanting custom backtests use the existing "New backtest" flow).

### Key Decisions

1. **Framing = "B-minus" / operational workflow smoke** (per Codex). Thin reuse of existing ingest + portfolio-backtest plumbing; no new product domain.
2. **Two-tier PASS check** decouples pipeline-health from business-metric flake (Codex). STRUCTURAL fails block CI exit; BUSINESS posts alerts only.
3. **CI placement = nightly + manual + optional gate**, NOT every-push gate. Realistic runtime budget makes every-push prohibitive.
4. **Canonical config is pinned in code**, not configurable from the UI button. CLI honors the same canonical config.
5. **CLI command = `msai backtest smoke`** (under existing backtest sub-app, near the artifact it produces).
6. **Result = normal Backtest row** tagged `smoke`, retrievable via existing `/api/v1/backtests/history?tag=smoke`.
7. **Warn surface = `/api/v1/alerts`** with `kind=smoke-warning`. Reuses existing alert plumbing.

### Open Questions (Remaining)

- [ ] Multi-strategy portfolio backtest dispatch path — does the existing `POST /api/v1/backtests/run` accept a multi-strategy payload, or does the smoke need to go through the `/api/v1/live-portfolios/` revision-based path with a `mode=backtest` flag? **Research-first agent should confirm in Phase 2.**
- [ ] Exact business-tier thresholds (Sharpe > X, P&L ±eps, min-trade-count) — placeholders in the design; real numbers chosen from an initial baseline run in Phase 5 implementation.
- [ ] Whether `msai backtest smoke` should ALSO accept `--re-ingest` and `--force` flags for operator overrides. Default behavior (skip ingest if Parquet present) is fixed; flags are convenience.
