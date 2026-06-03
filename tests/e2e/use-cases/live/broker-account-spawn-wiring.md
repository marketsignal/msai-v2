# Broker-account spawn-wiring — deploy by BrokerAccount identity + fail-closed validation

> Graduated from `/new-feature broker-account-spawn-wiring` (2026-06-02). Feature surfaces: API + CLI.
> Council-ratified narrowed Option A (`docs/decisions/multi-account-broker-fleet.md` 2026-06-02 addendum).
>
> **Verification status (2026-06-02):** the **fail-closed control-plane** behavior is verified end-to-end
> (UC-SW-API-2 PASS; UC-SW-CLI-1 refusal half PASS). The **active-account success path** (UC-SW-API-1 +
> UC-SW-CLI-1 success half) requires a **router-bound IB gateway**, which is the parked LVP/HVP
> market-hours operator drill — run those steps there. The success-path linkage/derivation is also
> covered by the integration suite (`backend/tests/integration/api/test_live_start_broker_account.py`).

---

## UC-SW-API-2 — Deploy against an archived account is refused before any node spawns (API) — PASS

**Actor:** Fleet operator integrating via the HTTP API.
**Scenario:** They archived a broker account but a deploy request still targets it; they expect a clean, specific refusal and nothing half-started.
**Interface:** API
**Intent:** The operator attempts to deploy against an unusable account and gets a fail-closed refusal, with no node left starting.
**Setup (sanctioned):** `POST /api/v1/broker-accounts` to create an active paper account, then `POST /api/v1/broker-accounts/{id}/archive`; create a portfolio + frozen revision via `POST /api/v1/live-portfolios/...`. Auth `X-API-Key`. Do NOT pre-create the deployment.
**Steps:**

1. `POST /api/v1/live/start-portfolio {portfolio_revision_id, broker_account_id:<archived>}`.
2. `GET /api/v1/live/status`.
   **Verification:** 422 with `detail.error.code == "BROKER_ACCOUNT_NOT_RESOLVABLE"` naming the account id; `GET /live/status` shows `active_count=0` and no deployment for that account (never a silent `starting`). A nonexistent-id control returns the same 422 (fail-closed on any non-active account).
   **Persistence:** Re-GET `/live/status` after a short delay → still refused/absent; stable.

## UC-SW-API-1 — Deploy to a selected active account; linked + identity derived (API) — control-plane covered by integration tests; live path = parked drill

**Actor:** Fleet operator integrating via the HTTP API.
**Scenario:** They added an active paper BrokerAccount and want to deploy a portfolio against it, confirming it's linked with identity derived from the row.
**Interface:** API
**Intent:** The operator deploys against a chosen broker account and retrieves it back showing the link + derived identity.
**Setup (sanctioned):** create an active paper BrokerAccount whose `ib_login_key` is **router-bound** (requires the gateway env — the drill env); create a portfolio + frozen revision.
**Steps:**

1. `POST /api/v1/live/start-portfolio {portfolio_revision_id, broker_account_id:<active>}`.
2. `GET /api/v1/live/status`.
   **Verification:** success + the deployment shows `broker_account_id == <selected>` and `account_id`/`ib_login_key` matching the account row (derived, not raw request); no TWS secret fields in the response.
   **Persistence:** Re-GET `/live/status` → still linked, identity unchanged.
   **Run env:** requires a router-bound gateway (parked LVP/HVP market-hours drill). Control-plane linkage/derivation is covered off-drill by `test_live_start_broker_account.py`.

## UC-SW-CLI-1 — Deploy from the CLI; clean refusal on a bad account (CLI) — refusal PASS; success = parked drill

**Actor:** Operator running the `msai` CLI on the host.
**Scenario:** They drive deployments from the shell — deploy to a good active account (works in the drill env), then try one against an archived account and expect a clear stderr refusal + non-zero exit, launching nothing.
**Interface:** CLI
**Intent:** The operator deploys via the CLI against a selected account and sees the same fail-closed behavior the API gives.
**Setup (sanctioned):** `msai broker add` an active paper account; create a portfolio/revision; archive a second account via `msai broker archive`.
**Steps:**

1. `msai live start-portfolio --revision <rev> --broker-account-id <good>` → succeeds (drill env: router-bound).
2. `msai live start-portfolio --revision <rev> --broker-account-id <archived>`.
   **Verification:** the archived invocation exits non-zero with stderr `API error (422): …BROKER_ACCOUNT_NOT_RESOLVABLE…`; `msai live status` (separate invocation) shows `Active nodes: 0` and no row for the archived account. The good invocation (drill env) prints the created deployment + `msai live status` lists it linked.
   **Persistence:** Fresh shell → `msai live status` still shows no deployment for the archived account.
   **Run env:** the success half (step 1 actually deploying) requires the router-bound gateway (parked drill); the refusal half is verified off-drill.
