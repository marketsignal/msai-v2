# Live deploy: `ib_login_key` is required + revision/account conflict surfaces 422

**Bug fixed:** drill-uncovered Bug #1 (live-deploy-safety-trio).
Before this PR, `POST /api/v1/live/start-portfolio` accepted requests
without `ib_login_key`; the API schema marked the field optional but the
database column was `NOT NULL`, so the insert raised `IntegrityError` → 500. Also, retrying the same `(portfolio_revision_id, account_id)` with
a different `ib_login_key` collided on the `uq_live_deployments_revision_account`
UNIQUE index and surfaced another opaque 500.

This PR makes the API reject both cases with explicit 422 responses.

## UC1.1 — Missing `ib_login_key` → 422 (not 500)

**Intent:** A client that forgets to send `ib_login_key` gets a Pydantic
validation error, not an opaque internal-server error.

**Interface:** API.

**Setup:** Backend is up at `http://localhost:8800`. Any portfolio
revision id is acceptable — the schema check fires before any DB read.

**Steps:**

```bash
curl -sf -X POST http://localhost:8800/api/v1/live/start-portfolio \
  -H "X-API-Key: ${MSAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_revision_id": "00000000-0000-0000-0000-000000000001",
    "account_id": "DU1234567",
    "paper_trading": true
  }'
```

**Verification:**

- HTTP status `422`.
- Response body contains a `detail` array (FastAPI's validation-error
  shape) with at least one entry whose `loc` ends in `ib_login_key` and
  `type` is `missing`.
- Sample (FastAPI 0.115):

  ```json
  {"detail": [{"type": "missing", "loc": ["body", "ib_login_key"], "msg": "Field required", ...}]}
  ```

**Persistence (negative):**

- `GET /api/v1/live/status` shows no new deployment row.
- `SELECT count(*) FROM live_deployments` is unchanged (operator can
  spot-check via the API's status endpoint; do not query Postgres
  directly per the project ARRANGE-vs-VERIFY rule).

## UC1.2 — Empty `ib_login_key` → 422

**Intent:** `ib_login_key: ""` is rejected with the same shape as a
missing field. The schema declares `min_length=1`.

**Steps:**

```bash
curl -sf -X POST http://localhost:8800/api/v1/live/start-portfolio \
  -H "X-API-Key: ${MSAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_revision_id": "00000000-0000-0000-0000-000000000001",
    "account_id": "DU1234567",
    "paper_trading": true,
    "ib_login_key": ""
  }'
```

**Verification:** HTTP `422`; `detail[*].loc` includes `ib_login_key`;
`type` is `string_too_short` (Pydantic v2 message).

## UC1.3 — Conflicting `(revision_id, account_id, login_key)` → 422 `LIVE_DEPLOY_CONFLICT`

**Intent:** Once a deployment exists for a `(portfolio_revision_id,
account_id)` pair, a second `/start-portfolio` call that differs only by
`ib_login_key` (or any other identity-bearing field) is rejected with a
clear 422 + operator hint, NOT a 500 IntegrityError.

**Interface:** API + paper IB.

**Setup:**

1. Seed a portfolio revision via the API (the standard create-revision
   flow).
2. Successfully deploy it once with `ib_login_key=user-a` against a
   paper account. Wait for `/status` to show the row as `running` (or
   stop it; status doesn't matter for the conflict test).

**Steps:** Issue a second start with the SAME `portfolio_revision_id`
and `account_id`, but a different `ib_login_key`:

```bash
curl -sf -X POST http://localhost:8800/api/v1/live/start-portfolio \
  -H "X-API-Key: ${MSAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_revision_id": "<same>",
    "account_id": "<same>",
    "paper_trading": true,
    "ib_login_key": "user-b"
  }'
```

**Verification:**

- HTTP `422`.
- Body shape:

  ```json
  {
    "detail": {
      "error": {
        "code": "LIVE_DEPLOY_CONFLICT",
        "message": "An existing deployment for this portfolio revision + account ...",
        "details": {
          "existing_deployment_id": "<uuid>",
          "existing_status": "running",
          "existing_ib_login_key": "user-a",
          "existing_paper_trading": true,
          "requested_ib_login_key": "user-b",
          "requested_paper_trading": true,
          "hint": "stop the existing deployment via POST /api/v1/live/stop, then retry"
        }
      }
    }
  }
  ```

**Persistence:**

- `GET /api/v1/live/status` still shows the original deployment only.
- No new row created.

## UC1.4 — Identity-signature stability under `ib_login_key`

**Intent:** The deployment's `identity_signature` MUST change when
`ib_login_key` changes. Otherwise the supervisor would reuse the wrong
trading subprocess / IB Gateway connection on a relogin.

**Interface:** Unit (no API call). This is the regression test that
proves the identity tuple includes `ib_login_key`. Lives in
`backend/tests/unit/test_deployment_identity_portfolio.py::TestPortfolioDeploymentIdentity::test_different_ib_login_key_produces_different_signature`.

**Steps:** Build two `PortfolioDeploymentIdentity` instances with
identical fields except `ib_login_key` (`user-a` vs `user-b`).

**Verification:** `id1.signature() != id2.signature()`.

## Why these aren't real-IB-required

Bug #1 is a schema + identity-correctness fix. Nothing in this PR
changes the live trading wire (no supervisor changes, no
TradingNodeSubprocess changes, no IB adapter changes). The 422 paths
fire at the FastAPI request boundary; the conflict path fires at the
ORM `SELECT` boundary. None of these need IB Gateway or a paper
account.

The full paper-money drill stays the operator's responsibility for the
Bug #2 / Bug #3 PRs that follow.
