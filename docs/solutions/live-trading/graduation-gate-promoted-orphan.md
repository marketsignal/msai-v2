# Graduation gate referenced a nonexistent stage ("promoted")

**Branch:** `fix/graduation-gate-promoted-orphan` (PR #\_\_)
**Date:** 2026-05-13
**Surfaced by:** paper-live drill step 5 (post-IB_PORT fix; council Option 3)

## Problem

`POST /api/v1/live-portfolios/{id}/strategies` rejected every strategy with HTTP 400 `Strategy {id} has no promoted GraduationCandidate`, regardless of which graduation stage the candidate was actually at. The paper drill could not progress past step 5; real-money trading would have hit the same blocker.

## Root Cause

`backend/src/msai/services/live/portfolio_service.py:_is_graduated` queried:

```python
GraduationCandidate.stage == "promoted"
```

But `backend/src/msai/services/graduation.py:VALID_TRANSITIONS` has **no** `"promoted"` stage. The actual state machine is:

```
discovery → validation → paper_candidate → paper_running → paper_review
              ↓                                                  ↓
            archived ←————————————————————————————————— live_candidate
                                                            ↓
                                                      live_running ↔ paused
```

The literal `"promoted"` was an orphan from a prior taxonomy that was replaced before the portfolio service even existed. Git history confirms: `services/graduation.py` shipped with the current 9-stage machine; `portfolio_service.py` was added later still using the stale string.

The bug was masked by every test suite that touched the gate: each test inserted fixture rows with `stage="promoted"` (matching the broken query), so the gate appeared to work. No prod row ever reached that stage because the state-machine API refused the transition.

## Solution

1. **New named constant** in `services/graduation.py`:

   ```python
   ELIGIBLE_FOR_LIVE_PORTFOLIO: frozenset[str] = frozenset({
       "live_candidate",
       "live_running",
       "paused",
   })
   ```

   Council 5-advisor + Codex chairman verdict (Option B, 3-of-5 with caveats): a strategy "has crossed the live-promotion boundary" iff its candidate is at `live_candidate` (approved for live), `live_running` (currently deployed), or `paused` (halted but resumable).

2. **Replace the broken literal**:

   ```python
   GraduationCandidate.stage.in_(ELIGIBLE_FOR_LIVE_PORTFOLIO)
   ```

3. **Defensive live-deploy block** at `POST /api/v1/live/start-portfolio` (Codex Contrarian's blocking objection #1): the gate only checks `strategy_id`, but portfolio members carry arbitrary `config + instruments`. Until a separate snapshot-binding follow-up verifies the member matches the approved candidate, `paper_trading=false` returns HTTP 503 `LIVE_DEPLOY_BLOCKED`. The guard fires BEFORE the idempotency layer so cached outcomes cannot replay.

4. **Stale-doc sweep:** updated module + class docstrings, error message, CLI help text, model docstring, and 6 test files using `stage="promoted"` as fixture data (replaced with `live_candidate` for live-portfolio tests; `paper_candidate` for the legacy backtest-portfolio orchestration test that isn't gated by `_is_graduated`).

## Prevention

Three regression guards land with the fix:

1. **`test_portfolio_service_graduation_gate.py::test_graduation_gate_matrix`** — parametrized over all 9 stages. Inserts a real Strategy + GraduationCandidate at each stage in a Postgres testcontainer and asserts `_is_graduated()` returns the correct accept/reject.

2. **`test_stage_matrix_covers_every_known_stage`** — asserts the parametrize list covers `GraduationService.ALL_STAGES` exactly. If someone adds a new stage to `VALID_TRANSITIONS` without classifying it here, this test fails. Forces synchronization between the gate definition and the state machine.

3. **`test_eligible_constant_is_subset_of_known_stages`** — asserts `ELIGIBLE_FOR_LIVE_PORTFOLIO ⊆ GraduationService.ALL_STAGES`. Catches the orphan-literal failure mode at the constant-definition site itself.

Plus:

4. **`TestStartPortfolioLiveBlocked::test_live_paper_trading_false_blocked_before_idempotency`** — replay test ensuring the live-block guard fires before the idempotency layer reserves anything. Catches the "cached outcome replays bypass" failure mode (Codex iter-2 P2).

## Operator runbook (post-merge)

To deploy a strategy to live (paper) trading after this fix:

1. Backtest the strategy: `POST /api/v1/backtests/run`.
2. Create graduation candidate: `POST /api/v1/graduation/candidates` with `{strategy_id, backtest_id, config, metrics}`.
3. Walk stages — each call: `POST /api/v1/graduation/candidates/{id}/stage` with `{stage: <next>}`:
   - `discovery` → `validation`
   - `validation` → `paper_candidate`
   - `paper_candidate` → `paper_running`
   - `paper_running` → `paper_review`
   - `paper_review` → `live_candidate` (now eligible for live portfolio)
4. Create portfolio: `POST /api/v1/live-portfolios`.
5. Add strategy: `POST /api/v1/live-portfolios/{id}/strategies`. Gate now PASSES.
6. Freeze revision: `POST /api/v1/live-portfolios/{id}/snapshot`.
7. Deploy: `POST /api/v1/live/start-portfolio` with `paper_trading=true`, `account_id=DU...`.

For real-money (`paper_trading=false`): blocked at the API with 503 until the snapshot-binding follow-up lands.

## Deferred follow-ups (post-merge)

- **Member-to-candidate snapshot binding:** start-portfolio must verify the portfolio member's `config + instruments + strategy_id` match the approved candidate snapshot. Removes the 503 guard added here.
- **`api/live.py:545` stage-transition logic:** the graduation-linking block should TRANSITION the candidate from `live_candidate` to `live_running` on first deploy, not search for already-running candidates with null `deployment_id`.
- **Archived membership invariants:** what happens to existing portfolio members when their candidate is archived?
- **Paused re-add semantics:** should a `paused` strategy be re-addable to a different draft revision, or must it return to `live_candidate` first?
- **API 500 → 409 on unique-violation:** portfolio-name uniqueness collision surfaces as 500 with raw SQL error. Should map to 409 per `.claude/rules/api-design.md`.
- **Forward-only transitions verification** (Contrarian objection #4): the state machine has backward edges (`paper_review → discovery`, `paused → live_running`). A targeted invariant test should clarify which regressions are legitimate.

## Lessons

- **Fixture parity hides production bugs.** Every test that touched `_is_graduated` inserted `stage="promoted"` fixtures matching the broken query. The bug only surfaced when a real graduation pipeline ran end-to-end. Lesson: integration tests that ARRANGE fixture data should derive stage values from production constants (`ELIGIBLE_FOR_LIVE_PORTFOLIO`) so a code-level rename forces test review.
- **Council preflight + paper drill earns its keep again.** The paper drill (council Option 3) found this within 30 minutes of starting; no unit test had caught it in months because the fixtures masked it.
- **Defensive guards are cheap before snapshot binding lands.** A 503 on live deploys is a 6-line guard. A misrouted real-money order is unrecoverable. Trade favorably.
