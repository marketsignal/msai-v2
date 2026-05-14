# Snapshot binding at /start-portfolio (Bug #3, replaces PR #63's 503 guard)

**Bug fixed:** Layer D of the live-deploy-safety-trio. PR #63 shipped
a temporary 503 `LIVE_DEPLOY_BLOCKED` guard that rejected ALL
`paper_trading=false` deploys, pending real per-member verification.
Without that verification a portfolio member could carry arbitrary
parameters that diverged from the approved `GraduationCandidate` —
sending real-money orders against an unapproved config. This PR
replaces the guard with real `verify_member_matches_candidate`.

## UC3 — Divergent member config → 422 BINDING_MISMATCH

**Intent:** A frozen portfolio member whose `config` differs from
the approved candidate's `config` is rejected with a clear diff, not
silently deployed.

**Interface:** API.

**Setup:**

1. Pick a strategy with a graduated `live_candidate`. The candidate
   carries a specific `config` (e.g. `{"instrument_id": "AAPL.NASDAQ",
"bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL", "instruments":
["AAPL.NASDAQ"]}`).
2. Create a fresh portfolio + add the strategy as a member with a
   DIFFERENT config (e.g. add `"fast_ema_period": 99` — a parameter
   the candidate doesn't have).
3. Freeze the revision via `POST /api/v1/live-portfolios/{pid}/snapshot`.

**Steps:**

```bash
curl -sf -X POST -H "X-API-Key: ${MSAI_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_revision_id": "<frozen-revision-id>",
    "account_id": "DUP733213",
    "paper_trading": true,
    "ib_login_key": "marin1016test"
  }' \
  http://localhost:8800/api/v1/live/start-portfolio
```

**Verification:**

- HTTP `422`.
- Response body:
  ```json
  {
    "detail": {
      "error": {
        "code": "BINDING_MISMATCH",
        "message": "Frozen portfolio member diverged from its graduated candidate: config",
        "details": {
          "member_id": "<uuid>",
          "candidate_id": "<uuid>",
          "mismatches": [
            {"field": "config", "member_value": {...}, "candidate_value": {...}}
          ]
        }
      }
    }
  }
  ```
- The `mismatches` array MUST name the divergent field(s).

**Persistence (negative):**

- `GET /api/v1/live/status` shows NO new deployment row.
- `live_deployments` count unchanged.

## UC4 — Matching config + paper_trading=true → deploy succeeds

**Intent:** When the frozen member matches the approved candidate
exactly (ignoring deploy-injected fields like `manage_stop`), live
deploy succeeds via the new binding flow. Pre-#63 was 200; PR #63 was
the 503 guard for `paper_trading=false`; this PR brings the 503 down
for verified deploys.

**Interface:** API + paper IB.

**Setup:**

1. Graduate `smoke_market_order` with config matching what we'll add.
2. Create portfolio + add member with the SAME `config` and same
   `instruments` as the candidate.
3. Snapshot the revision.

**Steps:** Same curl as UC3, with the matching revision.

**Verification:**

- HTTP `201` (created) or `200`.
- Response body carries `id`, `deployment_slug`, `status="starting"` (or `"running"` post-warm-up), `paper_trading: true`, `warm_restart: false`.

**Persistence:**

- `GET /api/v1/live/status?active_only=true` shows the deployment.
- The graduation candidate's `deployment_id` is set to the new deployment id, and `stage` is transitioned `live_candidate → live_running`.
- Re-running the SAME request (same Idempotency-Key) returns the cached outcome — `binding_fingerprint` was identical, so idempotency holds.

## UC5 — paper_trading=false no longer blocked (the headline)

**Intent:** The 503 `LIVE_DEPLOY_BLOCKED` guard from PR #63 is gone.
Real-money (`paper_trading=false`) deploys flow through the binding
verification just like paper. This is the WHOLE POINT of the PR.

**Interface:** API.

**Setup:** Same as UC4 but with `paper_trading=false`. Operator
explicit confirmation required for real-money exposure.

**Verification (negative — paper drill uses paper):** This UC is
called out in the use-case file so the operator KNOWS it must be
explicitly authorized. The paper-drill version uses `paper_trading=
true` because the only thing that's changing in the wire is the
guard removal; the binding code path is identical regardless. A
follow-up real-money drill (mslvp000 / U... test-lvp) confirms the
no-blocked behavior on the real path.

## UC6 — Idempotency replay with same body returns cached outcome

**Intent:** After a successful first deploy, replaying the SAME
request body with the SAME `Idempotency-Key` returns the cached
outcome. The `binding_fingerprint` is stable across the
`live_candidate → live_running` transition (by design — the
fingerprint excludes stage), so the body_hash matches and the
cached outcome serves.

**Interface:** API.

**Setup:** Run UC4 to success.

**Steps:** Replay the same `POST /start-portfolio` with the same
body + Idempotency-Key.

**Verification:**

- HTTP `200` (cached outcome) with the same deployment id.
- Response body identical to the first call.

**Persistence:** No new deployment row.

## UC7 — Candidate re-graduation invalidates cache

**Intent:** If the operator re-graduates the candidate with
different params BETWEEN replays, the `binding_fingerprint`
changes, the body_hash changes, and the idempotency layer treats
the second call as a NEW request — running binding verification
fresh.

**Interface:** API (manual graduation re-run via the API).

**Setup:** Run UC4 to success. Then via the API: create a NEW
graduation candidate for the same strategy with a different config
(e.g. different `fast_ema_period`). Walk it to `live_candidate`.

**Steps:** Replay the original `POST /start-portfolio` with the same
body + Idempotency-Key.

**Verification:**

- HTTP 422 (since the original portfolio member's config doesn't
  match the NEW candidate) OR 200 if the original candidate is
  still the linked one — depends on which candidate the warm-restart
  path resolves. Concretely: the FIRST deploy linked the original
  candidate via `deployment_id`. Warm-restart resolution picks the
  linked candidate. So the response is the SAME as the first call
  IF the linked candidate hasn't changed.
- The cache MISS only fires when the LINKED candidate's content
  changes (re-graduation creates a NEW candidate; the linked one
  is untouched unless explicitly archived).
- Concrete test: archive the original candidate, replay → expect
  422 `LIVE_DEPLOY_REPAIR_REQUIRED` (the linked candidate is gone
  but a new one exists; we don't auto-rebind).

**Persistence:** No new deployment row on the 422 path.

## Why these aren't real-IB-required

UC3 + UC6 + UC7 fire at the API request boundary + ORM SELECT path
— same as Bug #1's UC1. They don't need IB Gateway in the call graph.

UC4 + UC5 DO need IB Gateway to verify the deployment actually
spawns the trading subprocess after the binding check passes. Paper
account (DUP733213) is the sanctioned target per the
ARRANGE-vs-VERIFY contract.

## Drill ordering

1. UC3 first (fast, pure-API): proves binding rejection works.
2. UC4 second (requires IB Gateway): proves binding pass + actual
   subprocess spawn.
3. UC6 (replay): proves cache stability.
4. UC7 (archival): proves cache invalidation on candidate drift.
5. UC5 (real-money, optional): operator-confirmed real-money drill.
