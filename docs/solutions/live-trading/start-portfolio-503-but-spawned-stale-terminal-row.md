# start-portfolio returned 503 "unknown failure" while the spawn succeeded (stale terminal-row race)

**Status:** fixed on `fix/start-portfolio-503-but-spawned` (2026-06-05). E2E PASS on LVP live (API + CLI).
**Observed:** 2026-06-05 LVP drill — `POST /api/v1/live/start-portfolio` returned `503 {"detail":"unknown failure","failure_kind":"unknown"}` **3/3 times** while the node actually spawned (up, IB connected).

## Problem

On a **warm restart** (redeploying a deployment that ran before), the start endpoint's response told the operator the deploy failed when it had actually started fine. Worse, the false 503 was **cacheable** and got committed under the operator's Idempotency-Key, so honest retries with the same key returned the cached lie. Operator hazard: retry → double-deploy against a live node, or panic.

## Root Cause

`_poll_for_terminal` (`backend/src/msai/api/live.py`) polled _the latest `live_node_processes` row by `started_at desc`_ for the deployment with **no scoping to the current start attempt**:

1. The endpoint publishes START, then immediately polls (read-first loop).
2. The supervisor inserts the new `starting` row only after Redis delivery + Phase A locks/gates (`fleet_router.py:1936-1987`) — hundreds of ms later.
3. On warm restart, the **previous run's terminal row** (`stopped`/`failed`) is the latest row at first read → returned as "this attempt's outcome".
4. A cleanly-stopped row's `failure_kind` (NULL/`"none"`) coerces to `UNKNOWN` → `permanent_failure(UNKNOWN, "unknown failure")` → cacheable 503, committed under the Idempotency-Key.

Deterministic 3/3 because the stale row always exists at poll start and the supervisor insert always loses the race against the first poll read.

## Solution (approach A′ — hybrid attempt-scoped poll, contrarian-validated)

- `start_portfolio` snapshots the deployment's **existing terminal/stopping node-row ids** (`prior_node_row_ids`) between `db.commit()` and `bus.publish_start`.
- `_poll_for_terminal` gains `exclude_terminal_row_ids`: **terminal** rows count only if their id is NOT in the snapshot; **ready/running rows count unconditionally** — the partial unique index `uq_live_node_processes_active_deployment` guarantees at most one active row, and terminal rows never re-activate, so an active row IS the current node even if it predates the snapshot (concurrent-START case).
- PR #90 review refinement: the snapshot is narrowed to `("failed","stopped","stopping")` rather than every existing row, so raced-in active rows (`starting`/`building`/`ready`/`running` inserted by a concurrent START between the dedup check and the snapshot) stay observable and report their real `failure_kind` if they fail; `stopping` stays excluded because the active-process dedup check skips it, so an old draining node's `stopped` transition would otherwise be mistaken for this attempt's outcome.
- Exact id-set semantics — no clocks/timestamps (a `created_at > max(created_at)` marker has no tie-breaker; plan-review caught this).
- `/stop` and `/drain` call sites pass no snapshot → behavior unchanged. Supervisor untouched; no schema change.
- Sibling fixes shipped in the same branch: a latent `MissingGreenlet` 500 masking the honest 504 (the poll's per-iteration `db.rollback()` expires ORM attributes even with `expire_on_commit=False` — rollback always expires; use the plain `deployment_id` local), and `portfolio_revision_id` exposed on both live-status responses (operators could not rediscover the revision id needed to redeploy — found by verify-e2e as a broken sanctioned path).

## Prevention

- **Any response-path poll that attributes an outcome to a request MUST scope to rows created by that request** (or trust only states that are invariant-bound to "current", like the unique-index-guarded active set). "Latest row" is wrong whenever history persists.
- Test pattern that catches this class: integration test with a **pre-seeded terminal row** + a fake supervisor whose insert is **gated on observing the publish** (not a fixed sleep) — `tests/integration/api/test_live_start_broker_account.py::test_warm_restart_*`.
- Verify cacheability per outcome: a mis-attributed failure must never be committed under an Idempotency-Key (here: `permanent_failure` cacheable=True made the bug sticky).

## Environment gotchas hit while verifying (worth knowing)

- Running the dev stack **from a worktree**: use `docker compose -p msai-v2 …` so named volumes (DB state) are reused while bind mounts re-point to worktree code; AND symlink the worktree's `data/` → main repo `data/` — `TWS_SETTINGS_PATH` binds `./data/ib-gateway` relative to the compose file, and a fresh settings dir puts IB Gateway in **Read-Only mode** (error 321 → reconciliation fails → node never ready) because the accepted write-access precaution lives in the settings dir.
- E2E accounts: paper accounts are NOT provisioned/used (operator decision 2026-06-05) — live test accounts LVP (local) / HVP (prod VM) are the standard two-leg verification path.
