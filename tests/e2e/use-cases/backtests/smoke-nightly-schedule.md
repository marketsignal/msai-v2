# E2E Use Case — Nightly scheduled smoke + alert dispatch (CLI, smoke:nightly)

**Feature:** Operational workflow smoke (PRD `docs/prds/ingest-backtest-smoke-test.md` v1.3).
**Maps to:** PRD US-003.
**Interface:** CLI (the scheduled workflow drives the CLI on the VM; the alert lands in the existing `/api/v1/alerts` history, which the operator reads through the same surface).

---

## UC-SMK-003 — Nightly GitHub Actions schedule runs smoke:nightly on the VM and posts the metrics block to the alerts surface

**Actor:** Automated nightly job — the scheduled `.github/workflows/smoke.yml` workflow firing at 05:00 UTC, running unattended with read-only repo + workflow privileges and reusing `deploy.yml`'s SSH-into-VM credential model. (The operator reading the resulting alert the next morning is the consumer of the workflow's output.)

**Scenario:** The team needs structural pipeline regressions (broken ingest, broken Nautilus wiring, broken order plumbing) to wake someone up overnight instead of being discovered days later when the operator opens the dashboard. The nightly job runs `smoke:nightly` against the live VM stack and posts the structured metrics block to the alerts surface so the next morning's triage starts from a structured metrics view rather than an HTML report.

**Interface:** CLI

**Intent:** The scheduled job runs the deep canonical smoke against the prod VM overnight, exits non-zero only on structural failure, and leaves exactly one alert behind in `/api/v1/alerts` with the structured metrics block so the operator can triage at breakfast.

**Setup:**

1. The dev stack is running (`docker compose -f docker-compose.dev.yml up -d`) — for verification, "the VM" is the local dev stack reached over loopback; production runs against the prod VM via the existing deploy SSH path.
2. The Alembic smoke migration has run; the four `__smoke__/...` Strategy rows are seeded.
3. `MSAI_API_KEY` is exported (the same credential model `deploy.yml` uses).
4. The workflow is invoked via `gh workflow run smoke.yml` (verify-e2e simulates the scheduled trigger by manual dispatch — the cron string is a literal in YAML and cannot be retuned at runtime per PRD §5).
5. (Warm path) Parquet for AAPL + SPY for the full 2024 window is on disk; cold path adds full-year Databento fetch (≤ 60 min p95).
6. No `/api/v1/alerts` row currently exists for the smoke run id about to be created (verified via a pre-step `curl -sf -H "X-API-Key: $MSAI_API_KEY" "http://localhost:8800/api/v1/alerts/?kind=smoke-result&limit=1" | jq '.items[0].id'` returns either an older id or null).

**Steps:**

1. The job invokes `gh workflow run smoke.yml` (or waits for the cron trigger at 05:00 UTC).
2. The workflow's SSH stage runs `cd /opt/msai-v2/backend && uv run msai backtest smoke --config nightly --json` on the VM, captures the JSON output to a file, and pipes per-stage status lines to the workflow log.
3. The workflow's alert-dispatch stage runs `cd /opt/msai-v2/backend && uv run msai system smoke-alert /tmp/smoke-result.json` on the VM, which calls `AlertingService.send_alert(...)` directly (NOT through the read-only `/api/v1/alerts` endpoint — the alerts endpoint is a read surface).
4. The workflow exits — exit code matches the smoke CLI's STRUCTURAL tier.
5. The next morning, the operator runs `curl -sf -H "X-API-Key: $MSAI_API_KEY" "http://localhost:8800/api/v1/alerts/?kind=smoke-result&limit=5" | jq '.items[0]'` to see what the nightly produced.

**Verification:**

- After step 2, the workflow log captures the smoke CLI's per-stage status lines (`ingest-check`, `ingest`, `backtest-submit`, `backtest-poll`, `report-check`).
- After step 3, the operator's subsequent `GET /api/v1/alerts/?kind=smoke-result&limit=5` request receives a JSON response whose `items[0]` has:
  - `kind == "smoke-result"`,
  - a payload (or `metrics_block` field) containing `total_return`, `pnl`, `sharpe`, `sortino`, `alpha`, `beta`, `max_drawdown`, `trade_count_by_strategy` (a map keyed by strategy name with integer values), `trade_count_total`, `benchmark_symbol == "SPY"`, `smoke_config == "nightly"`, and a `backtest_id` referencing the PortfolioRun the nightly produced,
  - a `created_at` timestamp within the last few minutes of step 2.
- After step 4, the workflow run status in GitHub Actions reads `success` on structural PASS; `failure` on structural FAIL (including `trade_count_total < 2`).
- If business-metric drift was detected (e.g., Sharpe below baseline), the same alert payload's metrics block contains annotation fields (e.g., `sharpe_warning: "below_baseline_0.85"`) — no separate alert per drifted metric, no exit-code change.
- If the SSH path to the VM is unreachable, the workflow exits `failure` with the cause line `alert dispatch failed` or `VM unreachable` — and NO `kind=smoke-result` alert is posted (an infrastructure failure is distinguishable from a smoke signal).
- Exactly ONE new alert with `kind=smoke-result` is created per run (the operator's follow-up `GET /api/v1/alerts/?kind=smoke-result&limit=10` between two consecutive nightly runs shows exactly one new row).

**Persistence:** The next morning (or after a stack restart `docker compose -f docker-compose.dev.yml restart backend`), the operator re-issues `curl -sf -H "X-API-Key: $MSAI_API_KEY" "http://localhost:8800/api/v1/alerts/?kind=smoke-result&limit=10" | jq '.items[].id'` and the same alert row from step 5 is still listed at its original `created_at` position with the same id, the same metrics block, and the same `backtest_id` reference. The PortfolioRun the nightly produced is also still visible under `GET /api/v1/backtests/history?smoke_only=true` with `metrics.smoke_config == "nightly"`.

**Expected failure modes:**

- VM unreachable (SSH failure, NSG misconfiguration) → workflow exits non-zero with `alert dispatch failed: VM unreachable`; no `kind=smoke-result` row created.
- Backtest produces fewer than 2 trades (deterministic floor breached) → workflow exits non-zero; alert IS still posted (so the operator sees the metrics) but the alert's metrics block annotates the structural failure.
- Alert dispatch step fails (Postgres unreachable, AlertingService raises) → workflow exits non-zero with `alert dispatch failed: <cause>`; the smoke itself may have passed structurally, but the operator wakes up to the failure either way.

**Notes for verify-e2e:**

- For local verification, simulate the scheduled trigger via `gh workflow run smoke.yml` (`workflow_dispatch`); the cron literal is documentation, not runnable in verify-e2e.
- Verify the alert is queried via `GET /api/v1/alerts` (the read surface). The alert WRITE is via `AlertingService.send_alert` invoked by the `msai system smoke-alert` CLI — not by writing to `/api/v1/alerts` (which is read-only per PRD plan-review iter-1 P1-5).
- Warm-path nightly should finish in ≤ 10 min p95; cold path ≤ 60 min p95.

---
