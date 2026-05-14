# `ib_login_key` required + `(revision_id, account_id)` conflict gate

**Branch:** `fix/live-deploy-safety-trio` (Bug #1 of the three discovered
during the 2026-05-13 paper-money drill).

## Symptom

`POST /api/v1/live/start-portfolio` returned 500 Internal Server Error
in two distinct failure modes:

1. **Missing field:** the API schema marked `ib_login_key` as
   `Optional[str] = None`, but the `live_deployments.ib_login_key`
   column is `NOT NULL` (since PR #3). Any client that forgot to send
   the field reached `INSERT` and exploded with `IntegrityError:
null value in column "ib_login_key"`.
2. **Identity drift on retry:** an operator retrying a deployment with
   the same `(portfolio_revision_id, account_id)` but a different IB
   username (`ib_login_key`) hit the `uq_live_deployments_revision_account`
   UNIQUE index and got the same opaque 500. Worse: the prior
   `PortfolioDeploymentIdentity` tuple did NOT include `ib_login_key`,
   so two requests differing only by login key produced the SAME
   `identity_signature` — a silent footgun where the supervisor would
   reuse the wrong subprocess / IB Gateway connection on relogin.

## Root cause

API contract drift between PR #1 (added `ib_login_key` nullable),
PR #2 (populated it from request body), and PR #3 (enforced
`NOT NULL` on the column). The Pydantic schema was never re-tightened
to match the column. The deployment identity tuple was never extended
to include the login key.

## Fix

Three changes, all in this branch:

### 1. `backend/src/msai/schemas/live.py`

`ib_login_key: str = Field(min_length=1, max_length=64)` — required,
non-empty, bounded to the DB column width. Missing field surfaces as
a Pydantic `missing` error (HTTP 422). Empty string surfaces as
`string_too_short` (HTTP 422).

### 2. `backend/src/msai/services/live/deployment_identity.py`

`PortfolioDeploymentIdentity` gains an `ib_login_key: str` field;
`derive_portfolio_deployment_identity()` takes a corresponding
keyword arg. Two deployments of the same revision into the same account
via different IB logins now produce different `identity_signature`
values — the supervisor cannot silently reuse the wrong subprocess.

### 3. `backend/src/msai/api/live.py`

Two additions in `live_start_portfolio`:

- **Idempotency body hash** includes `ib_login_key` so replays from
  different login contexts don't share a cached outcome.
- **Pre-insert UNIQUE conflict gate:** before the
  `on_conflict_do_update` INSERT, query for an existing row by
  `(portfolio_revision_id, account_id)` where `identity_signature !=
new_signature`. If found, reject 422 with code
  `LIVE_DEPLOY_CONFLICT` and a `details` object identifying the
  existing deployment id, its status, both login keys, and a hint to
  stop the existing deployment first. Works for `running`, `starting`,
  `stopped`, AND `failed` rows — all still hold the UNIQUE slot.

## Verification

- **44 targeted unit tests** pass — schema validation paths (Pydantic
  missing/empty), identity-tuple distinctness, idempotency cache, and
  the new 422 conflict gate.
- **1862 full backend unit-test suite** pass.
- **`ruff check src/`** clean.
- **`mypy --strict src/`** clean (186 source files).
- **No paper IB drill required for this PR** — Bug #1 is a schema +
  identity-correctness fix. No live trading wire is touched (no
  supervisor changes, no `TradingNodeSubprocess` changes, no IB
  adapter changes). The 422 paths fire at the FastAPI request
  boundary; the conflict path fires at the ORM `SELECT` boundary.

E2E use case markdown: `tests/e2e/use-cases/live/start-portfolio-ib-login-key-required.md`
(UC1.1 missing → 422, UC1.2 empty → 422, UC1.3 conflict → 422 LIVE_DEPLOY_CONFLICT,
UC1.4 identity-signature stability).

## Deferred follow-ups (Bug #2 + Bug #3 in this trio)

This PR is **the first of three** carving up the drill-discovered safety
trio per a 5-advisor council recommendation (Codex chairman: split per
PR to limit blast radius + drill scope). The remaining work lives in
the design at `docs/plans/2026-05-13-live-deploy-safety-trio.md`:

- **Bug #2 (stop+flatness verification + TIF=DAY):** API `/stop` returns
  success without confirming Nautilus actually closed positions; IB
  account TIF preset mismatch on `market_exit`. Implementation is
  drafted (saved at `/tmp/bug2-backup/`) but needs a real paper IB
  drill before merging — memory entry `feedback_e2e_before_pr_for_live_fixes`
  is non-negotiable for live-wire changes. **Separate PR.**
- **Bug #3 (snapshot binding):** replace the PR #63 temporary 503
  `LIVE_DEPLOY_BLOCKED` guard with real config + instruments
  verification at start-portfolio time. Plan converged after 10
  Codex review iterations but Layer D is unstarted. **Separate PR
  after Bug #2.**

## Memory

Saved as `feedback_split_live_safety_pr_via_council.md` — when a single
branch accumulates multiple live-trading fixes and one is small +
correctness-only while the others need a real-money drill, council
recommends splitting per PR.
