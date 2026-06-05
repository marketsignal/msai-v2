# Live: warm-restart redeploy tells the truth (503-but-spawned regression)

> Graduated 2026-06-05 from `docs/plans/2026-06-05-start-portfolio-503-but-spawned.md` after verify-e2e PASS on the live stack (report `tests/e2e/reports/2026-06-05-19-21-start-portfolio-503-but-spawned.md`).
> Bug pinned: `POST /api/v1/live/start-portfolio` returned a false, cacheable `503 {"detail":"unknown failure","failure_kind":"unknown"}` 3/3 on warm restarts (2026-06-05 LVP drill) while the node spawned — the poll read the previous run's terminal `live_node_processes` row.
> **Account note (explicit per rails):** these UCs run on the LIVE test account **LVP `U4705114`** locally (`lvp` gateway, port 4003) — operator standing authorization 2026-06-05 ("no paper accounts"); post-merge second leg on HVP `U4715997` (prod VM). Short deploy→verify→stop cycles only; confirm flatness on stop. NOT for unattended cron execution.

## UC1 — API: warm-restart redeploy tells the truth `@smoke`

```
Actor:         Operator redeploying a live portfolio via the API after yesterday's session ended
Scenario:      Their portfolio ran yesterday and the node terminated overnight (clean stop or
               data-stale halt). This morning they redeploy the same frozen revision to the same
               account. Pre-fix this returned 503 "unknown failure" while the node actually
               spawned — the response must be truthful so they don't retry and double-deploy
               or panic.
Interface:     API
Intent:        The operator redeploys a previously-run portfolio and receives a truthful success
               response they can act on, instead of a false failure.
Account:       LIVE test account LVP `U4705114` via the `lvp` gateway (see header note).
Setup:         Dev stack up with the broker profile (lvp gateway connected, write-enabled). A
               deployable frozen portfolio revision exists with a prior terminal node-process run
               for its deployment (deploy once + stop via the sanctioned API if no prior run
               exists). The redeploy itself is the action under test — NOT performed in Setup.
Steps:         1) GET /api/v1/live/status → find the stopped deployment and read its
                  portfolio_revision_id (operator rediscovery — this read is part of the journey)
               2) POST /api/v1/live/start-portfolio {portfolio_revision_id, account_id,
                  ib_login_key, paper_trading:false} with a FRESH Idempotency-Key
               3) GET /api/v1/live/status
Verification:  The operator receives a success response (201 created-and-ready, or 200 already-
               active) whose body includes the deployment id and a ready/running status — NOT a
               503 "unknown failure"; the follow-up GET /live/status response includes that same
               deployment id listed as running, so they can proceed to monitor positions/trades.
               Replaying the SAME Idempotency-Key returns the cached success (not a stale failure).
Persistence:   Re-request GET /api/v1/live/status after ~30s — the deployment is still listed
               running with the same id. (Cleanup afterward: POST /api/v1/live/stop — expect
               clean stop, broker_flat true, no remaining positions.)
```

## UC2 — CLI: morning redeploy from the shell

```
Actor:         Operator driving the morning redeploy from the msai CLI on their laptop
Scenario:      Same warm-restart situation as UC1, but they work from the shell. Pre-fix the CLI
               surfaced the API's false 503 as a deploy error, leaving them unsure whether a node
               was live.
Interface:     CLI
Intent:        The operator redeploys from the CLI and the command reports success with the
               deployment identity; the status command then shows it running.
Account:       Same as UC1 — LIVE test account LVP `U4705114`.
Setup:         Same stack as UC1; UC1's cleanup stop leaves exactly the prior-run terminal state
               this UC needs (deploy once + stop via CLI or API if running standalone). CLI env:
               MSAI_API_URL=http://localhost:8800, MSAI_API_KEY set.
Steps:         1) Run `msai live status` → see the stopped deployment + its revision id
               2) Run `msai live start-portfolio --revision <id> --account U4705114
                  --ib-login-key lvp --no-paper --idempotency-key <fresh>` (answer the
                  real-money confirm prompt)
               3) Run `msai live status`
Verification:  start-portfolio stdout shows a success block with the deployment id/slug and NO
               "unknown failure" error, exit 0; the next invocation `msai live status` lists that
               deployment as running with a fresh heartbeat.
Persistence:   Run `msai live status` again in a separate invocation after ~20s — the deployment
               is still listed running with the same id. (Cleanup afterward: `msai live stop
               <deployment-id>` — positional arg — then confirm stopped via `msai live status`.)
```
