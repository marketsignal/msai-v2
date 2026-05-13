# Fix: graduation gate references nonexistent "promoted" stage

## Goal

`backend/src/msai/services/live/portfolio_service.py:_is_graduated` queries `GraduationCandidate.stage == "promoted"`, but the state machine in `backend/src/msai/services/graduation.py:VALID_TRANSITIONS` has NO `"promoted"` stage — the actual chain ends at `paused` / `live_running`. **No strategy can EVER be added to a live portfolio in the current codebase**, blocking the paper drill at step 5 and any real-money trading attempt.

## Architecture

The graduation state machine (`graduation.py:37-47`):

```
discovery → validation → paper_candidate → paper_running → paper_review
                                                              ↓
                                              live_candidate ← (or archived)
                                                   ↓
                                              live_running ↔ paused
                                                   ↓
                                              archived (terminal from any stage)
```

The portfolio_service's gate filters for `stage == "promoted"` — a string that exists in NO transition. The model docstring (`graduation_candidate.py:25`) still references an older taxonomy (`discovery → paper_validation → incubation → promoted`) that was replaced before the portfolio gate was even added. Git history confirms (per Contrarian's audit): `graduation.py` shipped with the current 9-stage machine; portfolio gate added later still using the stale `"promoted"` literal.

Council verdict (5 advisors + Codex chairman, 3-vs-2 split with Option B winning):
**Replace `stage == "promoted"` with `stage in ELIGIBLE_FOR_LIVE_PORTFOLIO = {"live_candidate", "live_running", "paused"}`.**

## Tech Stack

- `backend/src/msai/services/live/portfolio_service.py` (the broken gate at line 176; module docstring at line 4; error message at line 78)
- `backend/src/msai/services/graduation.py` (host of the new `ELIGIBLE_FOR_LIVE_PORTFOLIO` constant — same module that owns `VALID_TRANSITIONS`)
- `backend/src/msai/models/graduation_candidate.py` (stale docstring at line 25)
- `backend/src/msai/cli.py` (stale help text at line 518)
- 6 test files using `stage="promoted"` as fixture data (now invalid; must use a real stage)
- New `backend/tests/unit/services/live/test_portfolio_service_graduation_gate.py` — explicit stage matrix test

## Approach Comparison (council outcome)

| Axis                              | **B (chosen by 3 advisors): `{live_candidate, live_running, paused}`**                                        | A (2 advisors): `live_candidate` only                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Complexity                        | Single named constant + .in\_() query                                                                         | Single-string equality                                             |
| Blast Radius                      | Allows re-adding already-running or paused strategies to new portfolios                                       | Strictest gate — rejects already-running/paused                    |
| Reversibility                     | Tuple edit                                                                                                    | Same                                                               |
| Time to Validate                  | Unit-test matrix + drill resumption                                                                           | Same                                                               |
| User/Correctness Risk             | Lower — matches operator mental model "strategy has crossed the live-promotion boundary"                      | Higher friction — paused-then-resumed strategies need re-promotion |
| Codex Contrarian's deeper concern | Gate checks `strategy_id` but member carries arbitrary `config + instruments` — needs start-time revalidation | Same concern; orthogonal to which stage is accepted                |

## Contrarian Verdict

**COUNCIL** — full 5-advisor + Codex chairman ran (output captured in conversation transcript before this plan). Verdict: Option B with named constant, plus 4 blocking objections:

1. **Start-time revalidation (Contrarian's deeper concern):** gate checks `strategy_id` but portfolio member carries arbitrary `config + instruments`. Any live-qualified candidate could unlock a different config than what was graduated. The gate alone is NOT "real-money safe" without validation at `/api/v1/live/start-portfolio` that the frozen revision member's `config` + `instruments` match the approved candidate snapshot.
2. **DB audit/backfill** for any persisted `stage="promoted"` rows. **VERIFIED EMPTY on prod 2026-05-13** via `SELECT COUNT(*)... WHERE stage='promoted'` → 0 rows. The 2 prod candidates are at `paper_candidate` and `discovery`. No backfill needed.
3. **Archived membership + paused re-add invariants** clarification. Once a strategy is added to a frozen revision, what happens if its candidate is later archived? Can a `paused` strategy be re-added to a different draft revision without re-graduating? Deferred follow-up — not in scope of this PR.
4. **Forward-only graduation transitions** verification. **CORRECTION (Codex iter-2 P2):** the state machine is NOT strictly forward-only — `paper_review → discovery` and `paused → live_running` are both legitimate backward/sideways edges per `graduation.py:42`. Confirming "forward-only" requires a dedicated transition-graph invariant test (e.g. once a candidate has been live, can it regress to a pre-live stage? Should it?). Deferring this objection — the stage-matrix tests verify `_is_graduated` correctness, but do NOT verify transition invariants. Flag as deferred follow-up.

This PR addresses #2 (already satisfied — prod DB has 0 rows with `stage="promoted"`). #1, #3, and #4 are deferred follow-ups, **with a defensive guard added in this PR** (see Fix Design step 10 below): `/api/v1/live/start-portfolio` rejects `paper_trading=false` with HTTP 503 until the snapshot-binding follow-up lands. This means real-money deployment is blocked at the API boundary — Codex iter-1 P1 catch.

## Fix Design

### 1. Define `ELIGIBLE_FOR_LIVE_PORTFOLIO` in `graduation.py`

In `backend/src/msai/services/graduation.py`, alongside `VALID_TRANSITIONS`, add:

```python
# Stages at which a strategy is "eligible to be a member of a live portfolio"
# — i.e. has crossed the live-promotion boundary in the graduation lifecycle.
# This is NOT "safe to start trading without further validation"; the
# start-portfolio path must ALSO verify the frozen revision member's config
# + instruments match the approved graduation candidate snapshot
# (deferred follow-up; see docs/plans/2026-05-13-graduation-gate-promoted-orphan.md).
ELIGIBLE_FOR_LIVE_PORTFOLIO: frozenset[str] = frozenset({
    "live_candidate",
    "live_running",
    "paused",
})
```

### 2. Replace the broken literal in `portfolio_service.py`

```python
# was:
GraduationCandidate.stage == "promoted",
# becomes:
GraduationCandidate.stage.in_(ELIGIBLE_FOR_LIVE_PORTFOLIO),
```

Plus import the constant at the top of the file.

### 3. Update the user-facing error message at `portfolio_service.py:78`

```python
# was:
f"Strategy {strategy_id} has no promoted GraduationCandidate"
# becomes:
f"Strategy {strategy_id} has no GraduationCandidate at a live-eligible stage "
f"(one of: {sorted(ELIGIBLE_FOR_LIVE_PORTFOLIO)}). Run the graduation pipeline "
f"first: discovery → validation → paper_candidate → paper_running → paper_review → live_candidate."
```

### 4. Update module docstring at `portfolio_service.py:4` AND in-file docstrings at lines 37, 72 (Codex iter-1 P3)

Module docstring (line 4):

```python
# was:
- Only graduated strategies (promoted ``GraduationCandidate`` exists)
# becomes:
- Only graduated strategies (``GraduationCandidate`` exists at a live-eligible
  stage; see ``ELIGIBLE_FOR_LIVE_PORTFOLIO`` in ``graduation.py``)
```

Additional stale docstrings flagged by Codex iter-1 P3 that also need the "promoted" wording replaced with "live-eligible":

- `portfolio_service.py:37` — exception class docstring (`"Raised when adding a strategy that has no promoted..."`)
- `portfolio_service.py:72` — `add_strategy` docstring referencing "no promoted GraduationCandidate"

### 5. Update the model docstring at `graduation_candidate.py:25`

Replace the stale `discovery → paper_validation → incubation → promoted` with the real state machine, cross-referencing `VALID_TRANSITIONS`.

### 6. Update CLI help text at `cli.py:518`

Replace `discovery/paper/incubation/promoted` with the real stages.

### 7. Update 6 test files using `stage="promoted"`

The tests were inserting fixture rows with `stage="promoted"` AND the broken gate happened to query for the same string — so the tests PASSED while masking the bug. All 6 files now need to use a valid stage that's in `ELIGIBLE_FOR_LIVE_PORTFOLIO`. Recommend `stage="live_candidate"` everywhere (canonical "ready to deploy" semantic).

Files:

- `backend/tests/unit/test_cli.py:248,251,256,257` (CLI graduation filter test — also fix the assertion to use a real stage)
- `backend/tests/integration/test_portfolio_service.py:81`
- `backend/tests/integration/test_portfolio_full_lifecycle.py:84,174`
- `backend/tests/integration/test_revision_service.py:72`
- `backend/tests/integration/test_portfolio_deploy_cycle.py:102,281`
- `backend/tests/integration/test_portfolio_job_orchestration.py:109,116` — **NOTE (Codex iter-1 P3):** this is the legacy backtest-portfolio domain (analysis only — never gated by `_is_graduated`). The fixture stage there is "documented as existence-only, not stage-gated." Use `paper_candidate` instead of `live_candidate` so the test intent isn't misread as implying live approval.

### 8. New explicit stage-matrix unit test

Add `backend/tests/unit/services/live/test_portfolio_service_graduation_gate.py` with parametrized tests that enumerate EVERY stage from `VALID_TRANSITIONS` and assert which ones are accepted/rejected by `_is_graduated`. This is the regression guard requested by Scalability Hawk + Maintainer. Format:

```python
@pytest.mark.parametrize(
    ("stage", "expected_accepted"),
    [
        ("discovery", False),
        ("validation", False),
        ("paper_candidate", False),
        ("paper_running", False),
        ("paper_review", False),
        ("live_candidate", True),
        ("live_running", True),
        ("paused", True),
        ("archived", False),
    ],
)
async def test_graduation_gate_matrix(...): ...
```

This is the load-bearing test — covers all 9 stages with explicit accept/reject semantics. Plus TWO completeness assertions added per Codex iter-1 P2 (hardcoded parametrize list alone wouldn't fail on a new stage):

```python
def test_stage_matrix_covers_every_known_stage() -> None:
    """If someone adds a new stage to VALID_TRANSITIONS, this test fails
    until they explicitly classify it as accepted or rejected above."""
    from msai.services.graduation import GraduationService
    covered = {stage for stage, _ in _MATRIX_PARAMS}
    assert covered == GraduationService.ALL_STAGES, (
        f"matrix missing stages: {GraduationService.ALL_STAGES - covered}; "
        f"matrix has extra: {covered - GraduationService.ALL_STAGES}"
    )

def test_eligible_constant_is_subset_of_known_stages() -> None:
    """``ELIGIBLE_FOR_LIVE_PORTFOLIO`` must be a subset of the state machine;
    catches the orphan-literal failure mode that started this fix."""
    from msai.services.graduation import ELIGIBLE_FOR_LIVE_PORTFOLIO, GraduationService
    assert ELIGIBLE_FOR_LIVE_PORTFOLIO <= GraduationService.ALL_STAGES
```

### 10. Defensive guard: block `paper_trading=false` until snapshot binding lands (Codex iter-1 P1)

Without start-time validation that the portfolio member's `config + instruments` match the approved graduation candidate snapshot, a live deployment can use arbitrary parameters that bypass the spirit of graduation. To prevent shipping a real-money foot-gun:

In `backend/src/msai/api/live.py` `live_start_portfolio`, add the guard **BEFORE the idempotency layer** (Codex iter-2 P2 catch — otherwise a cached outcome could replay through without re-validating). The very first lines of the handler, before any `idem.reserve(...)` call:

```python
if not request.paper_trading:
    raise HTTPException(
        status_code=503,
        detail={
            "error": {
                "code": "LIVE_DEPLOY_BLOCKED",
                "message": (
                    "Live (paper_trading=false) deployments are temporarily blocked "
                    "pending the snapshot-binding follow-up (Codex Contrarian's blocking "
                    "objection #1 from the graduation-gate council, 2026-05-13). The "
                    "portfolio member's config + instruments must be verified against "
                    "the approved GraduationCandidate snapshot before real-money "
                    "execution can proceed. Track in docs/plans/."
                ),
            }
        },
    )
```

This is a temporary gate. When the snapshot-binding follow-up lands, this guard is removed and replaced by the actual binding check.

Tests:

- Unit test asserting `paper_trading=false` returns 503 with the documented error code.
- Unit test asserting `paper_trading=true` still proceeds (defensive — guard should NOT affect paper).
- **Idempotency-ordering regression (Codex iter-2 P2):** call start-portfolio twice with `paper_trading=false` + same `Idempotency-Key`; assert BOTH calls return 503 (guard fires before idempotency layer reserves anything; no CachedOutcome ever recorded).

### 9. The api/live.py:545 query (`stage == "live_running"`)

This query in the graduation-linking block (`live_start_portfolio`, after deployment creation) looks for a candidate ALREADY at `live_running` with `deployment_id IS NULL` — i.e. a candidate that was set to running by some prior path but never linked. Per Contrarian's analysis, this is logically inverted: the deployment should TRANSITION the candidate FROM `live_candidate` TO `live_running` on first deploy. Current code does neither. Out of scope for this PR — flag as deferred follow-up.

## E2E Use Cases (Phase 3.2b)

Project type: **fullstack**. Interfaces: API.

### UC1 — graduated strategy can be added to a live portfolio

**Intent:** A strategy at stage `live_candidate` (or `live_running` or `paused`) can be added to a draft live portfolio revision. A strategy at any pre-promotion stage (`discovery` through `paper_review`) is rejected with a clear error.

**Interface:** API.

**Setup (sanctioned ARRANGE):** auto-discovery seeds the strategies registry via filesystem sync; backtest a strategy via `POST /api/v1/backtests/run` → create graduation candidate via `POST /api/v1/graduation/candidates` → walk stages via `POST /api/v1/graduation/candidates/{id}/stage` from `discovery` → `validation` → `paper_candidate` → `paper_running` → `paper_review` → `live_candidate`.

**Steps:**

1. Create portfolio: `POST /api/v1/live-portfolios` with `{name: "uc1-test", description: "graduated-gate UC1"}`.
2. Add strategy to portfolio: `POST /api/v1/live-portfolios/{pid}/strategies` with `{strategy_id, config, instruments, weight}`.
3. Expect 201 Created. Get the portfolio: `GET /api/v1/live-portfolios/{pid}` shows 1 member.

**Negative case:**

1. Create another candidate at stage `paper_candidate` (don't promote further).
2. Try `POST /api/v1/live-portfolios/{pid}/strategies` with that strategy.
3. Expect 400 Bad Request with error message mentioning "live-eligible stage" and the allowed stages.

**Verification:** Response codes + error message content. `GET` round-trip shows the portfolio members.

**Persistence:** N/A — REST round-trip is the persistence test.

### UC2 — paper drill resumes after the fix

**Intent:** The paper drill that was halted at step 5 by this bug now completes through deployment. After the fix:

1. Smoke strategy candidate walked through stages to `live_candidate`.
2. Added to live portfolio.
3. Frozen revision deployed via `/api/v1/live/start-portfolio` with `account_id="DUP733213"`, `paper_trading=true`.
4. Supervisor subprocess spawns. (Order submission requires market hours + entitlement — out of UC2 scope.)

**Interface:** API + operational.

**Setup:** Fix deployed to prod. Broker profile up on the deployed branch.

**Steps:**

1. Walk smoke_market_order candidate to `live_candidate` (via existing graduation API).
2. Add to portfolio + freeze.
3. POST start-portfolio.

**Verification:** `GET /api/v1/live/status` shows the deployment in non-failed state. Supervisor log shows `live_supervisor_starting` + subprocess spawn.

## Plan Review (Phase 3.3)

To be filled by Claude self-review + Codex.

## Implementation Order

1. **Layer A unit test (RED):** add the parametrized stage-matrix test in `backend/tests/unit/services/live/test_portfolio_service_graduation_gate.py`. Expect RED on stages currently rejected by `"promoted"` literal (all of them).
2. **Layer B test update (will be RED until fixture stages change):** survey the 6 test files with `stage="promoted"`; expect them to FAIL after the fix lands because the gate now rejects "promoted" too.
3. **Fix:** add `ELIGIBLE_FOR_LIVE_PORTFOLIO` constant to `graduation.py`. Replace the broken literal in `portfolio_service.py` with `.in_(...)`. Update error message + module docstring.
4. **Sweep stale docs/tests:** model docstring, CLI help, 6 test fixture files (replace `"promoted"` with `"live_candidate"`).
5. **Re-run all tests** — expect GREEN.
6. **Codex code review** — Phase 5.1.
7. **Simplify** — Phase 5.2.
8. **Verify-app** — Phase 5.3.
9. **E2E (UC1 + UC2)** — Phase 5.4.
10. **Open PR** — Phase 6.4.
11. **After merge:** resume paper drill step 5 → 6 → 7.

## Deferred follow-ups (post-merge)

- **Member-to-candidate snapshot binding** (Contrarian's blocking objection #1): start-portfolio must verify the portfolio member's `config` + `instruments` match the approved graduation candidate.
- **api/live.py:545 stage-transition logic**: the graduation-linking block should TRANSITION the candidate from `live_candidate` to `live_running` on first deploy, not search for already-running candidates with null deployment_id.
- **Archived membership invariants**: clarify what happens to existing portfolio members when their candidate is archived.
- **Paused re-add semantics**: should a `paused` strategy be re-addable to a different draft revision, or must it return to `live_candidate` first?
- **API 500 → 409 on unique-violation**: portfolio-name uniqueness collision surfaces as 500 with raw SQL error. Should map to 409 per `.claude/rules/api-design.md`.
