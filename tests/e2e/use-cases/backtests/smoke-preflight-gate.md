# E2E Use Case — Opt-in `run_smoke=true` preflight on `deploy.yml` (CLI, smoke:fast)

**Feature:** Operational workflow smoke (PRD `docs/prds/ingest-backtest-smoke-test.md` v1.3).
**Maps to:** PRD US-004.
**Interface:** CLI (the operator invokes `gh workflow run deploy.yml` — a CLI command — and observes results via `gh run view` and the same `/api/v1/alerts` history; the deploy.yml workflow YAML wires the preflight in).

---

## UC-SMK-004 — Operator invokes a higher-risk deploy with `run_smoke=true` and the preflight gates the deploy stage

**Actor:** Operator (Pablo) about to deploy a higher-risk change to the prod Azure VM, working from his laptop with `gh` authenticated against the repo, intentionally opting in to the pre-deploy environment + data preflight.

**Scenario:** Pablo has just merged a change that touches the ingest path and wants extra sanity before the auto-deploy that fires on push to main lands the new image. He invokes `deploy.yml` manually with `run_smoke=true` so a `smoke:fast` runs on the VM against the CURRENTLY-DEPLOYED image first — proving the VM environment + Databento path + Nautilus pipeline are healthy before the deploy stage swaps the image. He does NOT expect this to validate the candidate code itself; that's deferred to a future staging-gate follow-up.

**Interface:** CLI

**Intent:** The operator opts in to a pre-deploy environment preflight; the deploy proceeds only when the smoke's structural tier passes, and stops cleanly before the deploy stage when it fails.

**Setup:**

1. Pablo's laptop has `gh` authenticated against the `msai-v2` repo with workflow-dispatch permission.
2. The dev stack is up (`docker compose -f docker-compose.dev.yml up -d`) — for verification, "the VM" is the local dev stack; production runs against the prod VM via the existing deploy SSH path.
3. The Alembic smoke migration has run on the target; the four `__smoke__/...` Strategy rows are seeded.
4. `MSAI_API_KEY` is exported.
5. (Warm path) Parquet for AAPL + SPY for the canonical `smoke:fast` window (2024-12-01 → 2024-12-31) is on disk so the preflight finishes inside the 3-min p95 warm budget.
6. The current `deploy.yml` workflow file on `main` accepts the `run_smoke` input (verified via `gh workflow view deploy.yml --yaml | grep -A1 'run_smoke:'` returning the input declaration with `default: false`).
7. A known good `<sha>` (a recent commit) is identified — typically the SHA the operator intends to deploy.

**Steps:**

1. From his laptop, Pablo runs `gh workflow run deploy.yml -f git_sha=<sha> -f run_smoke=true`.
2. `gh` reports the workflow has been queued; Pablo runs `gh run list --workflow=deploy.yml --limit 1 --json databaseId,status,conclusion` to capture the new run id.
3. Pablo runs `gh run watch <run-id>` (or polls `gh run view <run-id> --log`) and observes the workflow log.
4. On structural PASS, Pablo confirms the deploy stage proceeded by checking `gh run view <run-id> --log | grep -E "(preflight|deploy-on-vm)"` shows both stages ran in order.
5. On a deliberately broken stack (e.g., backend worker stopped via `docker compose -f docker-compose.dev.yml stop backtest-worker`), Pablo runs the same `gh workflow run deploy.yml -f git_sha=<sha> -f run_smoke=true` and confirms the deploy stage was SKIPPED — the smoke's structural FAIL stopped the workflow before the deploy stage ran.
6. After step 4 (the successful run), Pablo runs `curl -sf -H "X-API-Key: $MSAI_API_KEY" "http://localhost:8800/api/v1/alerts/?kind=smoke-result&limit=1" | jq '.items[0]'` to inspect the alert the preflight posted, if business-metric drift was annotated.

**Verification:**

- After step 1, the `gh workflow run` command reports the workflow has been queued and returns exit 0.
- During step 3, the workflow log shows a preflight job ran BEFORE the deploy stage, with stage lines `preflight = environment + data preflight, NOT candidate-image validation` clearly stated, followed by the smoke CLI's per-stage status lines (`ingest-check`, `ingest`, `backtest-submit`, `backtest-poll`, `report-check`).
- After step 4, the workflow concludes `success` AND the log confirms both stages ran (`preflight: success` THEN `deploy-on-vm: success`).
- After step 5, the workflow concludes `failure` AND the log shows the preflight stage failed with a specific cause line (e.g., `backtest-submit: FAIL — worker unreachable`), AND the deploy stage was SKIPPED — `gh run view <run-id> --log | grep -E "deploy-on-vm"` shows no `Run on VM` step output (the dependency on the preflight job stopped the deploy from running).
- After step 6, if business-metric drift was annotated, the `/api/v1/alerts` `items[0]` carries `kind == "smoke-result"`, the structured metrics block with annotation fields, and a `backtest_id` referencing the PortfolioRun the preflight produced.
- A subsequent `gh workflow run deploy.yml -f git_sha=<sha>` invocation WITHOUT `run_smoke=true` (or with `run_smoke=false`) shows the workflow log has NO preflight stage — the default path is unchanged from today's behavior (verified by `gh run view <newer-run-id> --log | grep -c "preflight"` returning 0).

**Persistence:** Pablo's laptop is closed and re-opened later. From a fresh shell with `gh` re-authenticated, he runs `gh run list --workflow=deploy.yml --limit 5 --json databaseId,status,conclusion,event` and sees both the successful preflight-gated run (step 4) and the deliberately-broken-stack run (step 5) still listed with their original conclusions (`success` and `failure` respectively) and `event: workflow_dispatch`. He then runs `gh run view <step-4-run-id> --log` and the preflight stage's per-stage status lines + the explicit "environment + data preflight, NOT candidate-image validation" framing line are still in the persisted log. The PortfolioRun the preflight created persists in `GET /api/v1/backtests/history?smoke_only=true` with `metrics.smoke_config == "fast"` after a stack restart.

**Expected failure modes:**

- Backtest worker container stopped → preflight fails with `backtest-submit: FAIL — worker unreachable`; deploy stage skipped; no new image deployed; alert posted via `AlertingService.send_alert(...)` so the operator sees the structural failure cause.
- Databento auth missing → preflight fails with `ingest: FAIL — DATABENTO_API_KEY not set`; deploy stage skipped.
- Preflight smoke exceeds the workflow job-timeout → workflow fails on timeout; deploy stage skipped.
- Operator forgets `-f run_smoke=true` on a deploy they meant to gate → no preflight runs (the opt-in is intentional); the deploy stage proceeds as it would for any normal push-triggered deploy.
- Operator passes `run_smoke=true` on a stack where the smoke migration has not yet run on the VM → preflight fails with `backtest-submit: FAIL — canonical smoke Strategy rows not found` (the runner's `_get_or_create_canonical_portfolio` check); operator runs `alembic upgrade head` on the VM before retrying.

**Notes for verify-e2e:**

- Local verification simulates the workflow_dispatch via the same `gh workflow run` CLI; production exercises the same path against the real VM and ACR.
- The preflight intentionally runs against the CURRENTLY-DEPLOYED image, NOT the candidate. The workflow log line `preflight = environment + data preflight, NOT candidate-image validation` is part of the acceptance criteria — verify it's present.
- The default auto-deploy on push to main is unchanged (no preflight) — verify by reading a recent push-triggered run's log and confirming the preflight stage is absent.

---
