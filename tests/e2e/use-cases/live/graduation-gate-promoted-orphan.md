# Graduation gate: live portfolios accept candidates at eligible stages

Graduated 2026-05-13 from `docs/plans/2026-05-13-graduation-gate-promoted-orphan.md`.
Source PR: `fix/graduation-gate-promoted-orphan` (PR #\_\_).
Regression for: 400 Bad Request on every add-strategy call because the gate queried a nonexistent `"promoted"` stage.

---

## UC1 — graduated strategy can be added to a live portfolio

**Intent.** A strategy whose `GraduationCandidate.stage` is in `ELIGIBLE_FOR_LIVE_PORTFOLIO = {live_candidate, live_running, paused}` can be added to a draft live portfolio. Any pre-promotion stage is rejected with a clear error.

**Interface.** API.

**Setup (sanctioned ARRANGE).** All setup via the public API:

1. Auto-registration sync seeds the `strategies` registry from `strategies/*.py` files (Phase 1 design — git-only).
2. Backtest the strategy: `POST /api/v1/backtests/run` with the strategy's id + config + instruments + window. Poll until `completed`.
3. Create graduation candidate: `POST /api/v1/graduation/candidates` with `{strategy_id, backtest_id, config, metrics}`.
4. Walk stages: `POST /api/v1/graduation/candidates/{id}/stage` with `{stage: <next>}`. Chain: `discovery → validation → paper_candidate → paper_running → paper_review → live_candidate`.

**Steps (happy path).**

1. Create portfolio: `POST /api/v1/live-portfolios` with `{name: "uc1-test-<ts>", description: "graduation-gate UC1"}`.
2. Add strategy: `POST /api/v1/live-portfolios/{pid}/strategies` with `{strategy_id, config, instruments, weight: "1.0"}`.

**Verification (happy path).**

- Step 2 returns 201 Created with a member id.
- `GET /api/v1/live-portfolios/{pid}` shows the member.
- Subsequent `POST /api/v1/live-portfolios/{pid}/snapshot` produces a frozen revision with `composition_hash`.

**Steps (negative case — pre-promotion stage).**

1. Create another candidate (different strategy) and STOP walking at `paper_candidate` (don't promote further).
2. Try `POST /api/v1/live-portfolios/{pid}/strategies` with that strategy.

**Verification (negative case).**

- Returns HTTP 400 Bad Request.
- Response body's error message contains `"live-eligible stage"` and the literal sorted list `['live_candidate', 'live_running', 'paused']`.
- Portfolio member count unchanged on `GET`.

**Persistence.** REST round-trip + DB write through service layer. No additional persistence assertion needed.

**Failure modes.**

- **400 on a strategy at `live_candidate`** → the fix did not deploy; verify backend image SHA on prod matches the merged PR SHA.
- **422 on `paper_trading=false`** is **expected** — see UC2 / the live-block guard.
- **500 on portfolio create with duplicate name** → known deferred follow-up (should be 409); harmless for this UC if test uses a unique name.

---

## UC2 — paper drill resumes after the fix (operational checklist)

**Intent.** The paper drill (council Option 3) halted at step 5 by the gate bug now reaches first-order placement.

**Interface.** API + operational.

**Setup.** Fix merged + auto-deployed. Broker profile up on the deployed branch:

```bash
sudo COMPOSE_PROFILES=broker docker compose \
  -f /opt/msai/docker-compose.prod.yml \
  --env-file /run/msai.env \
  --env-file /run/msai-images.env \
  up -d ib-gateway live-supervisor
```

IB Gateway healthy (login complete via gnzsnz). `IB_MARKET_DATA_TYPE=DELAYED` if `marin1016test` realtime entitlement still missing.

**Steps.**

1. Walk the existing smoke_market_order candidate (currently `paper_candidate` on prod) through the chain to `live_candidate`. 4 stage transitions.
2. Create live portfolio (or reuse the `paper-drill-2026-05-13` portfolio).
3. Add smoke strategy: `POST /api/v1/live-portfolios/{id}/strategies` with the smoke config + `weight=1.0`. Gate now PASSES.
4. Freeze revision: `POST /api/v1/live-portfolios/{id}/snapshot`.
5. Start: `POST /api/v1/live/start-portfolio` with `{portfolio_revision_id, account_id: "DUP733213", paper_trading: true}`.
6. Watch supervisor subprocess spawn + IB subscribe.
7. If market hours: smoke strategy submits one 1-share market-order. Verify via `GET /api/v1/live/positions` + `/trades`.
8. Stop: `POST /api/v1/live/stop`. Verify zero open orders + positions via API AND broker (IB portal).

**Verification.**

- Step 3: 201 Created with member id.
- Step 5: 201 Created with deployment_id; `GET /api/v1/live/status` shows the deployment in non-failed state.
- Step 7 (if market hours): position appears + later flattens.
- Step 8: zero positions both surfaces.

**Persistence.** Deployment, orders, trades persist in DB. Flatness verifiable by reloading status.

**Failure modes.** Treat every 5xx during live/paper flow as stop-the-world. Memory `feedback_e2e_before_pr_for_live_fixes` applies — new bugs found here become the next fix-bug branch.

---

## UC3 — real-money deployment blocked at API until snapshot-binding lands

**Intent.** `paper_trading=false` is rejected with HTTP 503 `LIVE_DEPLOY_BLOCKED` until the snapshot-binding follow-up lands. This is a defensive guard to prevent the gate's `strategy_id`-only check from being exploited via an arbitrary config in the portfolio member.

**Interface.** API.

**Setup.** Any valid `(portfolio_revision_id, account_id)`. No graduation needed — the guard fires before any DB work.

**Steps.**

1. `POST /api/v1/live/start-portfolio` with `{portfolio_revision_id, account_id: "U1234567", paper_trading: false}`.

**Verification.**

- HTTP 503 Service Unavailable.
- Body `detail.error.code == "LIVE_DEPLOY_BLOCKED"`.
- Body `detail.error.message` mentions "snapshot" + references `docs/plans/`.

**Persistence.** Negative — no deployment row should be created. `GET /api/v1/live/status` count unchanged.

**Failure modes.**

- **2xx** → the guard is missing or was bypassed. CRITICAL. Roll back.
- **5xx other than 503** → unrelated regression; investigate before retry.

**Replay regression.** Issue the same call twice with the same `Idempotency-Key` header. Both must return 503. If the second returns a CachedOutcome 2xx, the guard is positioned AFTER the idempotency layer — fix immediately.
