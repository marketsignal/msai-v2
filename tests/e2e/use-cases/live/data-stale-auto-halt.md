# Data-Stale Auto-Halt — E2E Use Cases

> **Graduated 2026-06-04** from the PR 1b plan
> (`docs/plans/2026-06-03-pr1b-data-stale-auto-halt.md`, `#### E2E Use Cases`)
> after feature-mode PASS (report:
> `tests/e2e/reports/2026-06-04-06-15-pr1b-data-stale-auto-halt.md`).
>
> **Halt-FIRING (`data_stale`) coverage:** the path where a stale feed actually
> _arms_ the halt is covered by the in-repo simulated harness
> (`backend/tests/unit/test_data_stale_monitor.py`) — dev/CI has no live
> Databento feed, so a real stale-trigger cannot be produced end-to-end here.
> These UCs cover the operator-observable surfaces (data-health read, manual
> halt cause attribution, fail-closed resume). **Live-fire validation of the
> firing path belongs to the LVP/HVP market-hours drill.**
>
> **Surface coverage decision:** API — Covered (UC-DS-API-1, UC-DS-API-2).
> CLI — Covered (UC-DS-CLI-1). UI — N/A: PR 1b is an operator fleet-safety
> surface (API-first / CLI-second per project ordering); the UI fleet view
> ships in PR 4 by ratified sequencing — no UI page exists or is added for
> data-health in this PR.

---

## UC-DS-API-1 — Operator checks feed warmth on a quiet fleet

- **Actor:** Fleet operator integrating via the HTTP API
- **Scenario:** No deployments are live yet this session. Before deploying real money, the operator wants to confirm the data-health surface answers (rather than erroring) and reflects the true (empty) feed state, so they can trust it during an incident.
- **Interface:** API
- **Intent:** The operator reads the fleet's data-feed health and gets a trustworthy, well-formed answer even with nothing running.
- **Setup:** Authenticated session (X-API-Key dev auth); ensure no active deployments (`GET /api/v1/live/status` shows none active).
- **Steps:** `GET /api/v1/live/data-health` → inspect body → `GET /api/v1/live/status` (cross-check no actives)
- **Verification:** Receives 200 with `feeds: []`, `fleet_halted` reflecting the real latch state, and no error; the response shape lets the operator script against it (fields documented above).
- **Persistence:** Re-request `GET /api/v1/live/data-health` after a delay — same well-formed empty result (no flapping, no 5xx).

---

## UC-DS-API-2 — Operator halts the fleet and resumes it safely

- **Actor:** Fleet operator running an incident drill via the HTTP API
- **Scenario:** The operator drills the emergency path before real money: halt the fleet manually, confirm the halt cause is attributed as a MANUAL halt (not data-stale), then resume and confirm the system verifies preconditions and clears every halt artifact.
- **Interface:** API
- **Intent:** The operator can halt and then resume the fleet, with the resume verifying it is safe and leaving no stale halt residue.
- **Setup:** Authenticated session; no active deployments (vacuous-warm resume path — the stale-refusal path is covered by the in-repo harness since dev has no live feed).
- **Steps:** `POST /api/v1/live/kill-all` → `GET /api/v1/live/data-health` (see `fleet_halted: true` + manual cause, NOT `data_stale`) → `POST /api/v1/live/resume` → `GET /api/v1/live/data-health`
- **Verification:** Resume returns 200 with the verified-preconditions summary; the follow-up data-health read shows `fleet_halted: false` and NO residual halt cause; the cause seen mid-drill was attributed manual (distinguishable from data_stale).
- **Persistence:** Re-request data-health after a delay — still unhalted, still no cause residue (the Task-5 bug fix observable).

---

## UC-DS-CLI-1 — Operator checks feed warmth from the CLI

- **Actor:** Operator from the CLI on the host
- **Scenario:** During an incident the operator is in a shell, not a dashboard; they need the same warmth answer the API gives, human-readable.
- **Interface:** CLI
- **Intent:** The operator runs one command and reads per-feed warmth + fleet halt state.
- **Setup:** Authenticated env (`MSAI_API_URL`, `MSAI_API_KEY`); stack running.
- **Steps:** Run `msai live data-health` → (if halted from a prior drill step) read the halt banner → run again after `/resume`
- **Verification:** stdout shows the table (or an explicit "no active feeds" line) and the fleet-halt banner with cause when halted; exit 0; stderr explains any API error with the status code.
- **Persistence:** A fresh shell invocation returns the same state — the command is a faithful read of the live system, not cached.
