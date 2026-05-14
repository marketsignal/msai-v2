# Snapshot binding replaces PR #63's 503 LIVE_DEPLOY_BLOCKED guard

**Status:** drill-pending (operator-gate). PR not yet opened.
**Branch:** `fix/snapshot-binding-replaces-503-guard`.
**Plan:** `docs/plans/2026-05-13-live-deploy-safety-trio.md` §Bug #3 / Layer D.
**Predecessors merged in this trio:** PR #64 (Bug #1 `ib_login_key` required + UNIQUE gate), PR #65 (Bug #2 `TIF=DAY` + supervisor flatness report).

## What this PR does

Replaces the temporary 503 `LIVE_DEPLOY_BLOCKED` guard from PR #63 with real per-member binding verification at `POST /api/v1/live/start-portfolio`. For every frozen portfolio member, the deploy path now:

1. Resolves the bound `GraduationCandidate` (warm-restart deterministic by `deployment_id`; first-deploy unique `live_candidate` with `deployment_id IS NULL`; retryable carve-out for unlinked existing deployments in `{starting, failed, stopped}`).
2. Canonicalizes both sides' `instruments` via `lookup_for_live(as_of_date=exchange_local_today())` so futures rolls and alias drift don't false-reject equivalent symbols.
3. Verifies `config` (minus deploy-injected `{manage_stop, order_id_tag, market_exit_time_in_force}`) AND `instruments` (sorted-set) match — raises 422 `BINDING_MISMATCH` with field-level diff if not.
4. Computes a stable `binding_fingerprint` and folds it into the idempotency `body_hash` so candidate drift (re-graduation, archive) invalidates cached outcomes naturally — same body+key after a re-graduation no longer cache-hits.
5. Inside `SELECT FOR UPDATE` on each candidate: re-verifies binding against the locked row (Codex round 1), re-canonicalizes BOTH sides at one fresh `as_of` (Codex round 3 — day-boundary safe), guards against concurrent cross-deployment relink (Codex round 3), links `candidate.deployment_id = deployment.id`, transitions stage `live_candidate → live_running` (or `paused → live_running`) via `GraduationService.update_stage`. Link happens BEFORE `publish_start` so a START on the bus always corresponds to a linked candidate row.
6. Wraps the entire link block + commit + `publish_start` in a try/except that flips `deployment.status = "failed"` on any failure (Codex round 3 — the supervisor's watchdog only sweeps `live_node_processes`, which has no row pre-publish; without this the deployment dangled in `starting`).

Also stamps `instruments` into `GraduationCandidate.config` at both research-promotion sites in `api/research.py` so newly-graduated candidates carry the binding contract by default. One-shot `scripts/backfill_candidate_instruments.py` repairs pre-Bug-#3 rows by reading `research_job.config["instruments"]`.

## Files touched

- `backend/src/msai/services/live/snapshot_binding.py` (new) — central helpers + typed exceptions.
- `backend/src/msai/api/live.py` — `_resolve_binding_for_start_portfolio` helper; link block; widened failure cleanup; removed 503 guard.
- `backend/src/msai/api/research.py` — promotion stamps `instruments` from `job.config`.
- `scripts/backfill_candidate_instruments.py` (new) — dry-run + apply.
- `backend/tests/unit/test_snapshot_binding.py` (new, 28 tests) + 15 tests added to `test_live_api.py` + 15 to `test_research_api.py`.
- `tests/e2e/use-cases/live/start-portfolio-snapshot-binding.md` (new) — UC3..UC7.
- `tests/e2e/reports/snapshot-binding-pure-api-20260514T052145.md` (new) — verify-e2e PARTIAL report (UC3 PASS live).
- `docs/CHANGELOG.md` — drill-pending [Unreleased] entry.

## Code-review iteration history

Five Codex review rounds against the uncommitted diff, all fixed in-branch ("no bugs left behind"):

- **Round 1** P1 — link AFTER deploy commit + START publish; pre-resolved candidate reused without `FOR UPDATE` re-read; P2 — instruments not canonicalized via `lookup_for_live`. **Fixed:** reorder link before publish; add `SELECT FOR UPDATE` re-query + re-verify; thread canonical instruments through verify + fingerprint.
- **Round 2** P1#1 — locked re-verify uses stale `c_canon`. P1#2 — supervisor watchdog scans `live_node_processes`, not `live_deployments` (deployment dangles in `starting` on link/publish failure). P2 — `candidate_instruments` raises as 500. P3 — resolver error envelope not surfaced. **Fixed:** recompute candidate canonical in locked block; (initial narrow) try/except wrapping commit+publish; 422 on missing-instruments; include `exc.to_error_message()` in resolver-failed body.
- **Round 3** P1 — overwrites another deployment's FK when two cold starts race. P2 — link failures still leave deployment in `starting` (round-2 try/except was too narrow). P2 — `BINDING_NOT_GRADUATED` swallows the `(revision_id, account_id)` collision case. P2 — pre-reserve `as_of` and locked-recheck `as_of` diverge across futures-roll day-boundary. **Fixed:** race-guard on `locked.deployment_id`; widened try/except over entire link block; pre-binding `LIVE_DEPLOY_CONFLICT` collision check; re-canonicalize BOTH sides at one `link_as_of` inside the locked block.
- **Round 4** P2 — warm-restart query returns None when existing deployment row exists but no candidate is linked yet (the upsert→link window OR a failed pre-link attempt); incorrectly raises `LIVE_DEPLOY_REPAIR_REQUIRED` for legitimate retries. **Fixed:** add `existing_unlinked_retry` carve-out — fall through to first-deploy lookup when existing deployment status is `{starting, failed, stopped}` AND candidate is unlinked; race-guard at link time prevents cross-deployment rebinding.
- **Round 5** — clean. Loop converged.

## What this PR does NOT do (deferred follow-ups)

- **Operator paper-IB drill** for UC4 (matching config deploys), UC6 (idempotency replay), UC7 (archived linked candidate). Authorial intent in the use-case file; pure-API UC3 verified live (HTTP 422 envelope confirmed); UC4/UC6/UC7 require IB Gateway + paper DUP733213.
- **Real-money UC5** — operator-only, explicit authorization required (mslvp000 / U-prefix test-lvp).
- **One-shot backfill** — operator must run `scripts/backfill_candidate_instruments.py` against the prod DB BEFORE shipping this PR; otherwise pre-Bug-#3 candidates surface as 422 `BINDING_INSTRUMENTS_MISSING` on their next deploy. Dry-run is the default; `--apply` commits.

## Why this needs an operator drill

Per memory `feedback_e2e_before_pr_for_live_fixes` (PR #62 hard-won lesson): for live-trading fixes touching the supervisor / `trading_node_subprocess` / live-deploy paths, unit tests + reviews + UC3-class API contract checks only confirm the static shape. The only real signal is a paper drill that:

1. Brings up `broker` profile + IB Gateway.
2. Deploys a graduated `smoke_market_order` strategy whose member config exactly matches the candidate.
3. Watches for `BarEvent` flow + (in UC4 with market orders) the `OrderFilled` event.
4. Stops cleanly and verifies broker is flat per Bug #2's flatness contract.

Until that drill runs green against this branch's binding wire, the PR is not safe to merge.

## Operator handoff checklist

1. `git fetch origin && git switch fix/snapshot-binding-replaces-503-guard` (or `git worktree add` if not yet present locally)
2. `cd backend && uv run alembic upgrade head`
3. `python scripts/backfill_candidate_instruments.py` (dry-run first; then `--apply`)
4. `COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d`
5. Verify paper account DUP733213 reaches IB Gateway on port 4004
6. Execute UC4 from `tests/e2e/use-cases/live/start-portfolio-snapshot-binding.md`
7. Optionally execute UC6 (replay) + UC7 (archive linked candidate)
8. Append operator results to `tests/e2e/reports/snapshot-binding-pure-api-20260514T052145.md`
9. Tell Claude "create the PR" → `gh pr create` from this branch
