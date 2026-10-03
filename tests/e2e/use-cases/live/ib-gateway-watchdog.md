# Use Cases — IB Gateway Watchdog (prod always-on)

Graduated 2026-06-10 from `docs/plans/2026-06-10-ib-gateway-watchdog.md` after VERDICT: PASS
(`tests/e2e/reports/2026-06-10-17-44-ib-gateway-watchdog.md`).

The watchdog is a prod host-systemd oneshot+timer that keeps the single prod `ib-gateway`
container's **IB session** up (not just container-up), auto-restarts it when idle+down,
holds off while a live deployment is active, and escalates+stops after an anti-flap restart
cap. The decision logic is a pure unit-tested FSM (`msai.services.gateway_watchdog.decide`)
driven from the host via the `msai system gateway-watchdog-tick` CLI.

**Surface coverage:** CLI (operator tick + reset) + API read-side (`GET /api/v1/alerts/`).
UI: N/A — prod host-systemd operational job with no UI surface.

**Run note (dev):** invoke the CLI in the dev container as
`docker exec msai-claude-backend uv run python -m msai.cli system …` (project installed
editable in dev). Prod uses bare `python -m msai.cli` (image built `--no-install-project`).
The `--dry-run` flag persists counters + decision + alert but suppresses the real
`docker compose --force-recreate`, so these UCs are safe to run on any stack. Reset state
between runs with `system gateway-watchdog-reset`.

---

## UC-CLI-1 — Operator manually runs a watchdog tick against a down gateway and sees the decision + persisted counter

- **Actor:** Operator from the CLI on the prod VM (or local stack) diagnosing gateway health.
- **Scenario:** The operator suspects the IB gateway is wedged and wants to confirm the watchdog would act, without waiting for the timer. They run a tick manually and expect it to (a) decide to restart when idle+down and (b) record that it did so, so a second tick reflects the advancing anti-flap counter.
- **Interface:** CLI.
- **Intent:** The operator confirms the watchdog correctly detects a down-but-idle gateway and tracks its remediation attempts.
- **Setup:** Stack up; reset watchdog Redis state via `msai system gateway-watchdog-reset` so the counter starts clean. (Do NOT pre-set the counter — that's what the tick advances.) Simulate a down gateway with the tick's dry-run inject flags — bare boolean form: `--inject-health down --inject-idle` (NOT `--no-container-running=false`; Typer dual-bool flags take no `=value`).
- **Steps:** Run `msai system gateway-watchdog-tick --dry-run --inject-health down --inject-idle` → run it a second time (separate invocation).
- **Verification:** First invocation's stdout shows a human-readable line naming the decision (e.g. `decision=RESTART reason=idle-down-restart restart_count=1`) with exit 0; the second invocation shows `restart_count=2` — i.e. the next invocation reflects the advanced counter. (`--dry-run` suppresses the real `docker compose` action but still persists counters + decision.)
- **Persistence:** Exit the shell, open a new one, run the same tick again → `restart_count` continues advancing from the persisted Redis state (e.g. to 3), not reset to 1.

## UC-API-1 — Operator polling the alerts feed sees the watchdog's escalation after persistent gateway failure

- **Actor:** Operator/integrator polling the operational alerts API.
- **Scenario:** The gateway has been failing to recover; the operator monitors `/api/v1/alerts/` and expects the watchdog to escalate to a CRITICAL "persistent failure — operator needed" alert (and stop restart-storming) so they know to intervene manually.
- **Interface:** API.
- **Intent:** The operator learns from the alerts feed that the watchdog has escalated a persistent gateway failure and stopped auto-restarting.
- **Setup:** Stack up; `msai system gateway-watchdog-reset`. Drive the watchdog past the restart cap (K=3) by running `msai system gateway-watchdog-tick --dry-run --inject-health down --inject-idle` K+1 = 4 times (each injected-down "restart" never recovers). (Do NOT write the alert directly — the tick must produce it.)
- **Steps:** Run the tick 4 times → `GET /api/v1/alerts/` (with `X-API-Key`).
- **Verification:** The K+1-th (4th) tick's stdout shows `decision=ESCALATE reason=escalate-restart-cap`. The `GET /api/v1/alerts/` response includes a CRITICAL entry whose title names the gateway watchdog persistent-failure escalation (`IB Gateway persistent failure — operator needed`) and whose message states auto-restart has STOPPED and operator action is needed.
- **Persistence:** Re-request `GET /api/v1/alerts/` after a short delay → the CRITICAL escalation alert is still listed with the same id and content.
