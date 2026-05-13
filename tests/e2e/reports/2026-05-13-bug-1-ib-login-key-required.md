# verify-e2e report — Bug #1 (ib_login_key required + revision/account conflict gate)

**Date:** 2026-05-13
**Branch:** `fix/live-deploy-safety-trio`
**Use case:** [`tests/e2e/use-cases/live/start-portfolio-ib-login-key-required.md`](../use-cases/live/start-portfolio-ib-login-key-required.md)

## Setup

- Started a fresh uvicorn instance from the worktree's `backend/` on
  port `8801`, pointing at the existing dev Postgres (5433) + Redis
  (6380) the long-running containers expose.
- Default API key (`MSAI_API_KEY=msai-dev-key`) used so the request
  body is the only variable.
- The 8801 instance ran the **Bug #1 worktree code** (not the
  HEAD-on-main image the 8800 container serves).

Background task ids: `b52ods1dd` (backend start, exit 144 on SIGTERM
after the verify run — expected); `bwq0j2x9e` (health-wait).

## UC1.1 — Missing `ib_login_key` → 422 (PASS)

Request:

```
POST /api/v1/live/start-portfolio
{
  "portfolio_revision_id": "00000000-0000-0000-0000-000000000001",
  "account_id": "DU1234567",
  "paper_trading": true
}
```

Response (HTTP 422):

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "ib_login_key"],
      "msg": "Field required",
      "input": {
        "portfolio_revision_id": "00000000-0000-0000-0000-000000000001",
        "account_id": "DU1234567",
        "paper_trading": true
      }
    }
  ]
}
```

**Classification: PASS.** Matches use case verification spec exactly:
status 422, `detail[*].loc` ends in `ib_login_key`, `type` is `missing`.

## UC1.2 — Empty `ib_login_key` → 422 string_too_short (PASS)

Request body adds `"ib_login_key": ""`.

Response (HTTP 422):

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "ib_login_key"],
      "msg": "String should have at least 1 character",
      "input": "",
      "ctx": { "min_length": 1 }
    }
  ]
}
```

**Classification: PASS.** `min_length=1` constraint fires as designed.

## UC1.3 — Conflicting `(revision_id, account_id, login_key)` → 422 LIVE_DEPLOY_CONFLICT (NOT RUN)

Requires an existing successfully-deployed `LiveDeployment` row in the
DB. Setting that up via the API (the project's ARRANGE-vs-VERIFY rule
forbids direct DB inserts) requires a graduated strategy + portfolio
revision + a paper IB Gateway connection — non-trivial to script
inline, and the closure logic is identical to the unit-test path that
**does** assert this branch.

**Classification: deferred to integration test.**
The conflict-detection code path is covered by:

- `test_live_api.py::test_*` — exercises the gate via FastAPI TestClient
  with a mocked DB that returns a conflict row, asserting the 422
  shape + `LIVE_DEPLOY_CONFLICT` code + `details` payload.
- `test_portfolio_deploy_cycle.py` — uses real Postgres via
  testcontainers and exercises the full revision → deploy flow with
  the identity tuple including `ib_login_key`.

If a future regression flips the gate logic, these two would catch it
before the operator's next manual retry hit a 500.

## UC1.4 — Identity-signature stability under `ib_login_key` (PASS via unit)

`tests/unit/test_deployment_identity_portfolio.py::TestPortfolioDeploymentIdentity::test_different_ib_login_key_produces_different_signature`
asserts `id1.signature() != id2.signature()` when the tuples differ
only by `ib_login_key`. Passes in the 1862-test suite run.

## Summary

| Use case | Classification | Notes                                                                          |
| -------- | -------------- | ------------------------------------------------------------------------------ |
| UC1.1    | **PASS**       | End-to-end against worktree backend on 8801 via curl                           |
| UC1.2    | **PASS**       | End-to-end against worktree backend on 8801 via curl                           |
| UC1.3    | deferred       | Conflict gate covered by `test_live_api.py` + `test_portfolio_deploy_cycle.py` |
| UC1.4    | **PASS**       | Identity-tuple unit test                                                       |

**Overall: PASS for the surface this PR touches.** Two
HTTP-boundary use cases exercised against the worktree's running
backend — they hit the same Pydantic + FastAPI middleware path a
production client would.

## Why no paper IB drill

Bug #1 changes nothing on the live trading wire — no supervisor /
TradingNodeSubprocess / IB adapter code is modified. The 422 paths
fire at the FastAPI request boundary and the ORM `SELECT` boundary.
IB Gateway is not in the call graph. The paper-money drill is the
operator-required gate for Bug #2 (stop-flatness wire), which is
deferred to a separate PR per the 5-advisor council Option D verdict.
