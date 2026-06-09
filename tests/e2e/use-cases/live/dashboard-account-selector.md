# E2E Use Cases — Dashboard Account Selector + Real-Money Deploy Gate (PR4)

Graduated 2026-06-09 (all PASS, safe subset). Surfaces: API, CLI, UI.

> **Safe-subset note (council-ratified 2026-06-09):** the real-money _matching-confirm DEPLOY_ happy-path (UC-API-1 step 3 / UC-CLI-1 step 2 / UC-UI-2 clicking "Deploy (REAL MONEY)") is reserved for ATTENDED operation (it opens an IB session / spawns a TradingNode on a real-money account). It is integration-covered (`backend/tests/integration/api/test_live_start_broker_account.py::test_real_money_deploy_confirm_match_passes_gate`). In regression/smoke runs, execute only the assertions below (422 rejections + UI gating) — do NOT complete a matching-confirm real-money deploy unattended.

---

## UC-API-1 — Real-money deploy demands a matching identity confirmation

- **Actor:** API integrator wiring a deploy script against the fund account.
- **Scenario:** They have a frozen portfolio revision and a registered real-money (fund) broker account. They must prove a wrong/absent/ambiguous confirmation is refused before scripting any real deploy.
- **Interface:** API
- **Intent:** The integrator confirms the platform refuses a real-money deploy unless the confirmation matches the account.
- **Setup:** Register a broker account with `account_class="real"` via `POST /api/v1/broker-accounts` (must be gateway-route-bound so row-state validation passes and the gate fires); obtain a frozen portfolio revision via the live-portfolio API. Do NOT pre-deploy.
- **Steps:** `POST /api/v1/live/start-portfolio` to the real account with `paper_trading:false`: (1) NO `confirm_account_id`; (2) WRONG `confirm_account_id`; (3) `broker_account_id` + a conflicting legacy `account_id`.
- **Verification:** (1) → 422 `REAL_MONEY_CONFIRM_REQUIRED` (message names the account); (2) → 422 `REAL_MONEY_CONFIRM_MISMATCH`; (3) → 422 `AMBIGUOUS_DEPLOY_TARGET` (names resolved vs request target). Each error body is actionable.
- **Persistence:** Re-request `GET /api/v1/live/status` — no new deployment row for any rejected attempt (count unchanged).

## UC-CLI-1 — Operator deploys to the fund from the CLI with identity confirmation

- **Actor:** Operator running `msai live start-portfolio` on the toolbox.
- **Scenario:** They need the CLI to carry the same server-enforced identity confirmation so a fat-fingered account can't slip through.
- **Interface:** CLI
- **Intent:** The operator confirms the CLI deploy to a fund account is refused server-side without the identity confirmation.
- **Setup:** Register the real-money account via API; obtain a frozen revision; export `MSAI_API_URL`/`MSAI_API_KEY` (or run in the backend container).
- **Steps:** Run `msai live start-portfolio --revision <id> --broker-account-id <fund-uuid> --no-paper` WITHOUT `--confirm-account-id` (answer the typed live-trading prompt `y`).
- **Verification:** The server returns 422 and the CLI surfaces `REAL_MONEY_CONFIRM_REQUIRED` to stderr with a non-zero exit; `--confirm-account-id` appears in `--help`.
- **Persistence:** `msai live status` lists no new deployment for the attempt.

## UC-UI-1 — Operator focuses the dashboard on one account and it sticks

- **Actor:** Fleet operator managing multiple accounts from the web dashboard.
- **Scenario:** With multiple accounts live, they focus the fleet on one account and expect that focus to survive navigation + reload.
- **Interface:** UI
- **Intent:** The operator scopes the dashboard to one account and trusts it persists.
- **Setup:** Authenticate (dev bypass); ≥1 registered account + ≥1 deployment (seed via API).
- **Steps:** Open `/live-trading` → pick an account in the top-bar selector (`account-scope-selector`) → navigate away + back → full reload.
- **Verification:** The fleet table re-scopes to that account; the selector trigger reads the account label; after navigation AND full reload the selection persists (`localStorage["msai.accountScope"]`) with no hydration console error.
- **Persistence:** Reload `/live-trading` → selector + scoped fleet still as chosen.

## UC-UI-2 — Operator deploys to an explicit, confirmed target (never implicit)

- **Actor:** Fleet operator about to deploy a revision.
- **Scenario:** With the global selector on "All", they must be forced to explicitly pick a target — and, for the fund, type-confirm it — before Deploy enables.
- **Interface:** UI
- **Intent:** The operator can only reach a deployable state after explicitly choosing and (for the fund) confirming the target.
- **Setup:** Authenticate; register a real-money (fund) account + a paper account via API; compose+backtest a portfolio and open its run-results page; global selector on "All". Do NOT pre-select a target.
- **Steps:** From the run results, "Deploy as Live Portfolio" → complete the promote modal → in the deploy dialog: observe Deploy disabled with "pick a target" → pick the fund (`portfolio-start-target-account`) → observe the "REAL FUND … LIVE MONEY" label + typed-confirm field → type a wrong id, then the exact id.
- **Verification:** With "All", the Preview/Deploy action is disabled + "Pick a target account to deploy" shows; the fund option is unmistakably "⚠ REAL FUND — … — LIVE MONEY"; the typed-confirm "Deploy (REAL MONEY)" stays disabled on a wrong id and enables only on the exact match; a paper account shows NO typed-confirm field (explicit target suffices). (Do NOT click Deploy — attended-only.)
- **Persistence:** Re-open the dialog with the selector on "All" → the disabled-until-target gating re-demonstrates.
